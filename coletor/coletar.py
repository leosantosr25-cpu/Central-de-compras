
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Coletor de ofertas dos supermercados (roda no GitHub Actions, de graça).
 
Para cada loja em LOJAS:
  1. abre a página com um navegador de verdade (Playwright), o que resolve sites com JavaScript;
  2. se a página tem texto com preços ("R$"), manda o texto para a IA (Gemini);
  3. baixa as imagens de encarte novas e manda para a IA ler produto e preço;
  4. se não achou nem texto nem imagens, tira "prints" da página e manda para a IA.
 
O resultado vai para dados/ofertas.csv (colunas: data, supermercado, produto, preco, link),
que o painel lê na aba "Sites dos mercados".
"""
import base64
import csv
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
 
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
 
RAIZ = Path(__file__).resolve().parent.parent
DADOS = RAIZ / "dados"
FUSO = ZoneInfo("America/Sao_Paulo")
CAMPOS = ["data", "supermercado", "produto", "preco", "link"]
VIEW_W = 1366
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
 
# Os nomes abaixo são reconhecidos pelo painel ("Tatico" vira "Supermercado Tatico", etc.).
# excluir_contexto: expressão regular; imagens cujo texto vizinho combine com ela são ignoradas
# (o site da Costa mostra encartes de várias cidades; aqui ficam só os de Goiânia).
LOJAS = [
    {"nome": "Tatico", "url": "https://taticobaratotododia.com.br/gyn/ofertas/"},
    {"nome": "Pro Brazilian", "url": "https://www.probrazilian.com.br/ofertas"},
    {"nome": "Assaí", "url": "https://www.assai.com.br/ofertas/goias/assai-independencia"},
    {
        "nome": "Costa Atacadão",
        "url": "https://costaatacadao.com.br/ofertas/",
        "excluir_contexto": r"Distrito Federal|Valpara|Rio Verde|Luzi[aâ]nia|Senador Canedo|Trindade",
    },
]
 
# Limites para ficar dentro da camada gratuita da IA (podem ser mudados no workflow).
LIMITE_CHAMADAS = int(os.environ.get("LIMITE_CHAMADAS") or 40)   # chamadas à IA por execução
PAUSA = float(os.environ.get("PAUSA_ENTRE_CHAMADAS") or 6)        # segundos entre chamadas
DIAS_HISTORICO = 60                                               # quantos dias ficam no CSV
 
PROMPT = (
    "Você lê ofertas de supermercado do Brasil. Extraia TODOS os produtos que tenham preço visível. "
    "Responda somente com um array JSON; cada item no formato "
    '{"produto": "nome com marca e tamanho quando aparecerem (ex.: Arroz Branco Tio João 5kg)", '
    '"preco": número em reais (ex.: 26.9)}. '
    "Regras: use o preço da embalagem/unidade como anunciado (só use preço por kg quando for o único); "
    "ignore itens sem preço legível; não invente nada; sem texto fora do JSON; se não houver ofertas, responda []."
)
 
JS_IMAGENS = r"""
() => {
  const BAD = /logo|icon|favicon|sprite|avatar|pixel|loading|placeholder/i;
  const THUMB = /-\d{2,3}x\d{2,3}\.(jpe?g|png|webp)/i;
  const ehImg = (u) => /\.(jpe?g|png|webp)(\?|$)/i.test(u || '');
  const contexto = (el) => {
    let n = el, t = '';
    for (let i = 0; i < 5 && n; i++, n = n.parentElement) {
      t = (n.innerText || '').trim();
      if (t.length > 15) break;
    }
    return t.slice(0, 400);
  };
  const achados = new Map();
  document.querySelectorAll('img').forEach(img => {
    if (img.closest('header, footer, nav')) return;
    const a = img.closest('a');
    let url = '', pelaLink = false;
    if (a && ehImg(a.href)) { url = a.href; pelaLink = true; }
    else { url = img.currentSrc || img.src || img.getAttribute('data-src') || ''; }
    if (!ehImg(url) || BAD.test(url) || THUMB.test(url) || achados.has(url)) return;
    if (!pelaLink && (img.naturalWidth || 0) < 500) return;
    achados.set(url, contexto(a || img));
  });
  return Array.from(achados, ([url, ctx]) => ({ url, ctx }));
}
"""
 
JS_ROLAR = """
async () => {
  const esperar = (ms) => new Promise(r => setTimeout(r, ms));
  for (let i = 0; i < 40; i++) {
    window.scrollBy(0, 900);
    await esperar(250);
    if (window.innerHeight + window.scrollY >= document.body.scrollHeight - 5) break;
  }
  window.scrollTo(0, 0);
}
"""
 
 
def log(*args):
    print(*args, flush=True)
 
 
def hoje_iso():
    return datetime.now(FUSO).date().isoformat()
 
 
class CotaEsgotada(Exception):
    pass
 
 
class ErroFatalIA(Exception):
    """Erro que se repetiria em toda chamada (modelo inexistente, chave inválida...)."""
 
 
# ---------------------------------------------------------------- IA (Gemini)
def chamar_gemini(partes):
    chave = os.environ.get("GEMINI_API_KEY")
    if not chave:
        raise RuntimeError("GEMINI_API_KEY não configurada")
    modelo = os.environ.get("GEMINI_MODEL") or "gemini-3.8-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent"
    corpo = json.dumps({
        "contents": [{"parts": partes}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
    }).encode("utf-8")
    dados = None
    for tentativa in range(4):
        req = urllib.request.Request(
            url, data=corpo,
            headers={"Content-Type": "application/json", "x-goog-api-key": chave},
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                dados = json.loads(r.read())
            break
        except urllib.error.HTTPError as e:
            detalhe = e.read()[:600]
            if e.code in (400, 401, 403, 404):
                texto_erro = detalhe.decode("utf-8", "replace").replace("\n", " ")
                raise ErroFatalIA(f"Gemini HTTP {e.code} (modelo '{modelo}'): {texto_erro}")
            if e.code == 429:
                if tentativa >= 1:
                    raise CotaEsgotada(f"limite da IA atingido (HTTP 429): {detalhe!r}")
                time.sleep(30)
                continue
            if e.code in (500, 503) and tentativa < 3:
                time.sleep(15 * (tentativa + 1))
                continue
            raise RuntimeError(f"Gemini HTTP {e.code}: {detalhe!r}")
    try:
        texto = dados["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"Resposta sem conteúdo da IA: {str(dados)[:300]}")
    return json.loads(texto)
 
 
def converter_preco(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    s = re.sub(r"R\$|\s", "", str(v or ""))
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None
 
 
def normalizar_resposta(resp):
    if isinstance(resp, dict):
        for v in resp.values():
            if isinstance(v, list):
                resp = v
                break
        else:
            return []
    if not isinstance(resp, list):
        return []
    saida = []
    for it in resp:
        if not isinstance(it, dict):
            continue
        nome = str(it.get("produto") or "").strip()
        preco = converter_preco(it.get("preco"))
        if not nome or len(nome) > 140 or preco is None:
            continue
        if not (0.2 <= preco <= 5000):  # descarta leituras absurdas
            continue
        saida.append((nome, round(preco, 2)))
    return saida
 
 
def ler_com_ia(partes, run):
    """Devolve a lista de (produto, preco), ou None se não foi lido (limite atingido)."""
    if run["parar"] or run["chamadas"] >= LIMITE_CHAMADAS:
        return None
    if run["chamadas"] > 0:
        time.sleep(PAUSA)
    run["chamadas"] += 1
    try:
        return normalizar_resposta(chamar_gemini(partes))
    except CotaEsgotada as e:
        run["parar"] = True
        log("  ! " + str(e))
        return None
    except ErroFatalIA as e:
        run["parar"] = True
        run["erro_ia"] = str(e)[:500]
        log("  ! ERRO NA IA (a coleta foi interrompida): " + run["erro_ia"])
        return None
 
 
# ---------------------------------------------------------------- páginas
def baixar_imagem(page, url):
    r = page.request.get(url, timeout=60000)
    if not r.ok:
        raise RuntimeError(f"HTTP {r.status}")
    tipo = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
    if tipo not in ("image/jpeg", "image/png", "image/webp"):
        ext = url.split("?")[0].rsplit(".", 1)[-1].lower()
        tipo = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(ext)
    corpo = r.body()
    if not tipo or len(corpo) < 5000 or len(corpo) > 15 * 1024 * 1024:
        return None
    return tipo, corpo
 
 
def parte_imagem(tipo, corpo):
    return {"inline_data": {"mime_type": tipo, "data": base64.b64encode(corpo).decode("ascii")}}
 
 
def coletar_loja(page, loja, estado, run, hoje):
    nome, url = loja["nome"], loja["url"]
    lidos = estado["lidos"]
    resumo = {"imagens": 0, "imagens_novas": 0, "ofertas": 0}
    ofertas = []  # (produto, preco, link)
 
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except PWTimeout:
        pass
    page.evaluate(JS_ROLAR)
    page.wait_for_timeout(1000)
 
    texto = page.inner_text("body")
    imagens = page.evaluate(JS_IMAGENS)
    if loja.get("excluir_contexto"):
        rx = re.compile(loja["excluir_contexto"], re.I)
        imagens = [i for i in imagens if not rx.search(i.get("ctx") or "")]
    resumo["imagens"] = len(imagens)
    log(f"  texto: {len(texto)} caracteres, {texto.count('R$')} símbolos R$; imagens candidatas: {len(imagens)}")
 
    leu_algo = False
 
    # 1) texto da página (mais barato e mais preciso que imagem)
    if texto.count("R$") >= 5:
        leu_algo = True
        chave = f"texto:{nome}:{hashlib.sha1(texto.encode('utf-8')).hexdigest()[:16]}"
        if chave not in lidos:
            res = ler_com_ia([{"text": PROMPT + "\n\nTexto da página:\n" + texto[:30000]}], run)
            if res is not None:
                lidos[chave] = hoje
                ofertas += [(p, v, url) for p, v in res]
 
    # 2) imagens de encarte ainda não lidas
    novas = [i for i in imagens if "img:" + i["url"] not in lidos]
    resumo["imagens_novas"] = len(novas)
    for img in novas:
        leu_algo = True
        try:
            baixada = baixar_imagem(page, img["url"])
            if baixada is None:
                lidos["img:" + img["url"]] = hoje  # ícone/arquivo estranho: não tentar de novo
                continue
            res = ler_com_ia([{"text": PROMPT}, parte_imagem(*baixada)], run)
        except Exception as e:  # uma imagem com problema não derruba a loja inteira
            log(f"  ! imagem {img['url'][-60:]}: {str(e)[:150]}")
            continue
        if res is None:
            break  # limite de chamadas/cota: o resto fica para a próxima execução
        lidos["img:" + img["url"]] = hoje
        ofertas += [(p, v, img["url"]) for p, v in res]
 
    # 3) sem texto de preços nem imagens: "prints" da página
    if not leu_algo and not imagens:
        chave = f"tela:{nome}:{hoje}"
        if chave not in lidos:
            altura = int(page.evaluate("document.documentElement.scrollHeight"))
            completo = True
            for y in range(0, min(altura, 9600), 2400):
                h = min(2400, altura - y)
                png = page.screenshot(clip={"x": 0, "y": y, "width": VIEW_W, "height": h},
                                      full_page=True, type="jpeg", quality=70)
                res = ler_com_ia([{"text": PROMPT}, parte_imagem("image/jpeg", png)], run)
                if res is None:
                    completo = False
                    break
                ofertas += [(p, v, url) for p, v in res]
            if completo:
                lidos[chave] = hoje
 
    resumo["ofertas"] = len(ofertas)
    return resumo, ofertas
 
 
# ---------------------------------------------------------------- arquivos
def carregar_estado():
    try:
        e = json.loads((DADOS / "estado.json").read_text("utf-8"))
        if isinstance(e.get("lidos"), dict):
            return e
    except Exception:
        pass
    return {"lidos": {}}
 
 
def podar_estado(estado, hoje):
    limite = (datetime.fromisoformat(hoje) - timedelta(days=90)).date().isoformat()
    estado["lidos"] = {k: v for k, v in estado["lidos"].items() if str(v) >= limite}
 
 
def ler_csv():
    caminho = DADOS / "ofertas.csv"
    if not caminho.exists():
        return []
    with open(caminho, newline="", encoding="utf-8") as f:
        return [dict(l) for l in csv.DictReader(f)]
 
 
def gravar_csv(linhas):
    with open(DADOS / "ofertas.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CAMPOS)
        w.writeheader()
        for l in linhas:
            w.writerow({c: l.get(c, "") for c in CAMPOS})
 
 
def mesclar(existentes, novas, hoje):
    limite = (datetime.fromisoformat(hoje) - timedelta(days=DIAS_HISTORICO)).date().isoformat()
    vistos, saida = set(), []
    for l in existentes + novas:
        if str(l.get("data", "")) < limite:
            continue
        chave = (l["data"], l["supermercado"], l["produto"].strip().lower(), str(l["preco"]))
        if chave in vistos:
            continue
        vistos.add(chave)
        saida.append(l)
    saida.sort(key=lambda l: (l["data"], l["supermercado"], l["produto"]))
    return saida
 
 
def main():
    if not os.environ.get("GEMINI_API_KEY"):
        log("ERRO: crie o segredo GEMINI_API_KEY no repositório (Settings > Secrets and variables > Actions).")
        return 1
    DADOS.mkdir(exist_ok=True)
    hoje = hoje_iso()
    estado = carregar_estado()
    run = {"chamadas": 0, "parar": False, "erro_ia": ""}
    novas, resumo = [], {}
 
    with sync_playwright() as pw:
        navegador = pw.chromium.launch(args=["--no-sandbox"])
        contexto = navegador.new_context(
            locale="pt-BR", timezone_id="America/Sao_Paulo",
            viewport={"width": VIEW_W, "height": 900}, user_agent=USER_AGENT,
        )
        for loja in LOJAS:
            log(f"== {loja['nome']} ({loja['url']})")
            page = contexto.new_page()
            try:
                resumo[loja["nome"]], ofertas = coletar_loja(page, loja, estado, run, hoje)
                for produto, preco, link in ofertas:
                    novas.append({"data": hoje, "supermercado": loja["nome"], "produto": produto,
                                  "preco": f"{preco:.2f}", "link": link})
                log(f"  -> {resumo[loja['nome']]}")
            except Exception as e:
                resumo[loja["nome"]] = {"erro": str(e)[:200]}
                log(f"  ! ERRO: {str(e)[:300]}")
            finally:
                page.close()
        navegador.close()
 
    podar_estado(estado, hoje)
    (DADOS / "estado.json").write_text(json.dumps(estado, ensure_ascii=False, indent=1), "utf-8")
    gravar_csv(mesclar(ler_csv(), novas, hoje))
    (DADOS / "ultima-execucao.json").write_text(json.dumps({
        "quando": datetime.now(FUSO).isoformat(timespec="seconds"),
        "chamadas_ia": run["chamadas"],
        "limite_atingido": run["chamadas"] >= LIMITE_CHAMADAS,
        "erro_ia": run["erro_ia"],
        "ofertas_novas": len(novas),
        "lojas": resumo,
    }, ensure_ascii=False, indent=1), "utf-8")
    log(f"Concluído: {len(novas)} oferta(s) lida(s), {run['chamadas']} chamada(s) à IA.")
 
    if run["erro_ia"] and not novas:
        return 1  # a IA recusou as chamadas: deixa a execução vermelha para você ser avisado
    if resumo and all("erro" in r for r in resumo.values()):
        return 1  # todas as lojas falharam: deixa a execução vermelha para você ser avisado
    return 0
 
 
if __name__ == "__main__":
    sys.exit(main())
