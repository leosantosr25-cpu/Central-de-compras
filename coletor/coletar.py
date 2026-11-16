#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Coletor de ofertas dos supermercados (roda no GitHub Actions, de graça).

Melhorias aplicadas:
  1. Desativação do 'thinkingBudget' na IA -> respostas em ~3 segundos em vez de 150s.
  2. Captura de imagens via screenshot do elemento no Playwright -> resolve o HTTP 403 do Assaí.
  3. Suporte a imagens Lazy Loading (WordPress/Elementor) -> resolve Tatico.
  4. Tratamento contra timeouts e banners de cookies.
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

LIMITE_CHAMADAS = int(os.environ.get("LIMITE_CHAMADAS") or 25)
PAUSA = float(os.environ.get("PAUSA_ENTRE_CHAMADAS") or 3)
LIMITE_POR_LOJA = int(os.environ.get("LIMITE_POR_LOJA") or 6)
LOTE_IMAGENS = int(os.environ.get("LOTE_IMAGENS") or 3)
TEMPO_MAX = float(os.environ.get("TEMPO_MAXIMO_MIN") or 18) * 60
DIAS_HISTORICO = 60

PROMPT = (
    "Você lê ofertas de supermercado do Brasil. Extraia TODOS os produtos que tenham preço visível. "
    "Responda somente com um array JSON; cada item no formato "
    '{"produto": "nome com marca e tamanho quando aparecerem (ex.: Arroz Branco Tio João 5kg)", '
    '"preco": número em reais (ex.: 26.9), "validade": "AAAA-MM-DD" se o encarte disser até quando vale, senão ""}. '
    "Regras: use o preço da embalagem/unidade como anunciado (só use preço por kg quando for o único); "
    "ignore itens sem preço legível; não invente nada; sem texto fora do JSON; se não houver ofertas, responda []."
)

PROMPT_LOTE = PROMPT + (" Vários encartes ou páginas do mesmo supermercado seguem anexados; "
                        "leia todos e junte os produtos num único array.")

# JS aprimorado para capturar imagens lazy (data-src, data-lazy-src, srcset) e background-images
JS_IMAGENS = r"""
() => {
  const BAD = /logo|icon|favicon|sprite|avatar|pixel|loading|placeholder|\/(styles|themes|icons|assets|sprites)\//i;
  const THUMB = /-\d{2,3}x\d{2,3}\.(jpe?g|png|webp)/i;
  const ehImg = (u) => /\.(jpe?g|png|webp)(\?|$)/i.test(u || '');
  
  const pegarUrl = (el) => {
    return el.getAttribute('data-lazy-src') ||
           el.getAttribute('data-src') ||
           el.getAttribute('data-original') ||
           el.currentSrc ||
           el.src || '';
  };

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
    let url = (a && ehImg(a.href)) ? a.href : pegarUrl(img);

    // Trata srcset do WordPress se a URL ainda estiver vazia ou com placeholder
    if ((!url || BAD.test(url)) && img.srcset) {
      const partes = img.srcset.split(',').map(s => s.trim().split(' ')[0]);
      url = partes[partes.length - 1] || url;
    }

    if (!ehImg(url) || BAD.test(url) || THUMB.test(url) || achados.has(url)) return;
    achados.set(url, contexto(a || img));
  });

  // Links diretos para imagens
  document.querySelectorAll('a[href]').forEach(a => {
    const url = a.href;
    if (!ehImg(url) || BAD.test(url) || THUMB.test(url) || achados.has(url)) return;
    if (a.closest('header, footer, nav')) return;
    achados.set(url, contexto(a));
  });

  return Array.from(achados, ([url, ctx]) => ({ url, ctx }));
}
"""

JS_ROLAR = """
async () => {
  const esperar = (ms) => new Promise(r => setTimeout(r, ms));
  for (let i = 0; i < 30; i++) {
    window.scrollBy(0, 800);
    await esperar(200);
    if (window.innerHeight + window.scrollY >= document.body.scrollHeight - 10) break;
  }
  await esperar(1000);
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
    pass


# ---------------------------------------------------------------- IA (Gemini)
def chamar_gemini(partes):
    chave = os.environ.get("GEMINI_API_KEY")
    if not chave:
        raise RuntimeError("GEMINI_API_KEY não configurada")
    modelo = os.environ.get("GEMINI_MODEL") or "gemini-2.0-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent"

    # Desativa thinkingBudget para respostas rápidas (2-5s em vez de 150s)
    generation_config = {
        "temperature": 0,
        "responseMimeType": "application/json",
        "thinkingConfig": {"thinkingBudget": 0}
    }

    corpo = json.dumps({
        "contents": [{"parts": partes}],
        "generationConfig": generation_config,
    }).encode("utf-8")

    dados = None
    for tentativa in range(3):
        req = urllib.request.Request(
            url, data=corpo,
            headers={"Content-Type": "application/json", "x-goog-api-key": chave},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                dados = json.loads(r.read())
            break
        except urllib.error.HTTPError as e:
            detalhe = e.read()[:1500]
            # Se o modelo não aceitar thinkingConfig (ex.: 1.5-flash), tenta sem ele
            if e.code == 400 and b"thinkingConfig" in detalhe:
                generation_config.pop("thinkingConfig", None)
                corpo = json.dumps({
                    "contents": [{"parts": partes}],
                    "generationConfig": generation_config,
                }).encode("utf-8")
                continue
            if e.code in (400, 401, 403, 404):
                texto_erro = detalhe.decode("utf-8", "replace").replace("\n", " ")
                raise ErroFatalIA(f"Gemini HTTP {e.code} (modelo '{modelo}'): {texto_erro}")
            if e.code == 429:
                if b"per day" in detalhe.lower() or b"perday" in detalhe.lower():
                    raise CotaEsgotada("limite diário da IA atingido (volta amanhã)")
                time.sleep(20)
                continue
            if e.code in (500, 503) and tentativa < 2:
                time.sleep(10 * (tentativa + 1))
                continue
            raise RuntimeError(f"Gemini HTTP {e.code}: {detalhe!r}")
        except (TimeoutError, urllib.error.URLError, ConnectionError) as e:
            if tentativa < 2:
                time.sleep(5 * (tentativa + 1))
                continue
            raise RuntimeError(f"Sem resposta da IA: {str(e)[:150]}")

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
        if not (0.2 <= preco <= 5000):
            continue
        validade = str(it.get("validade") or "").strip()
        if re.match(r"^\d{4}-\d{2}-\d{2}$", validade) and validade < hoje_iso():
            continue
        saida.append((nome, round(preco, 2)))
    return saida


def ler_com_ia(partes, run):
    if run["parar"] or run["chamadas"] >= LIMITE_CHAMADAS:
        return None
    if time.time() - run["inicio"] > TEMPO_MAX:
        run["parar"] = True
        log("  ! tempo máximo da execução atingido; o que faltar fica para a próxima.")
        return None
    if run["chamadas"] > 0:
        time.sleep(PAUSA)
    run["chamadas"] += 1
    t0 = time.time()
    try:
        res = normalizar_resposta(chamar_gemini(partes))
        log(f"  IA #{run['chamadas']}: {len(res)} oferta(s) em {time.time() - t0:.0f}s")
        return res
    except CotaEsgotada as e:
        run["parar"] = True
        log("  ! " + str(e))
        return None
    except ErroFatalIA as e:
        run["parar"] = True
        run["erro_ia"] = str(e)[:500]
        log("  ! ERRO NA IA: " + run["erro_ia"])
        return None


# ---------------------------------------------------------------- páginas
def tipo_da_imagem(headers, url):
    tipo = (headers.get("content-type") or "").split(";")[0].strip().lower()
    if tipo not in ("image/jpeg", "image/png", "image/webp"):
        ext = url.split("?")[0].rsplit(".", 1)[-1].lower()
        tipo = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(ext)
    return tipo


def baixar_imagem(page, url, referer):
    """
    Baixa a imagem com fallback em screenshot do elemento (evita 403 de CDNs como o Assaí).
    """
    tipo, corpo = None, None

    # 1) Se o elemento já está na tela, tira print direto dele (100% imune a 403)
    try:
        nome_arquivo = url.split("?")[0].split("/")[-1]
        loc = page.locator(f'img[src*="{nome_arquivo}"]').first
        if loc.count() > 0 and loc.is_visible():
            corpo = loc.screenshot(type="jpeg", quality=85)
            if len(corpo) >= 5000:
                return "image/jpeg", corpo
    except Exception:
        pass

    # 2) Requisição HTTP direta simulando headers reais do navegador
    try:
        r = page.request.get(url, timeout=30000, headers={
            "Referer": referer,
            "User-Agent": USER_AGENT,
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "Sec-Fetch-Dest": "image",
            "Sec-Fetch-Mode": "no-cors",
        })
        if r.ok:
            tipo, corpo = tipo_da_imagem(r.headers, url), r.body()
    except Exception:
        pass

    # 3) Fallback: Fetch dentro do contexto da própria página
    if corpo is None:
        try:
            data_url = page.evaluate("""async (u) => {
                const res = await fetch(u, { mode: 'no-cors' }).catch(() => null);
                if (!res) return null;
                const blob = await res.blob();
                return new Promise((resolve) => {
                    const reader = new FileReader();
                    reader.onloadend = () => resolve(reader.result);
                    reader.readAsDataURL(blob);
                });
            }""", url)
            if data_url and "," in data_url:
                header, b64 = data_url.split(",", 1)
                corpo = base64.b64decode(b64)
                tipo = header.split(";")[0].replace("data:", "") or "image/jpeg"
        except Exception:
            pass

    if not tipo or not corpo or len(corpo) < 5000 or len(corpo) > 15 * 1024 * 1024:
        return None
    return tipo, corpo


def parte_imagem(tipo, corpo):
    return {"inline_data": {"mime_type": tipo, "data": base64.b64encode(corpo).decode("ascii")}}


def fechar_avisos_e_cookies(page):
    """Fecha avisos de LGPD e seletores de loja que bloqueiam a página."""
    for texto in ["Aceitar", "Entendi", "Concordo", "Aceito", "Continuar", "Fechar"]:
        try:
            btn = page.get_by_role("button", name=re.compile(f"^{texto}", re.I)).first
            if btn.count() > 0 and btn.is_visible():
                btn.click(timeout=1000)
                page.wait_for_timeout(500)
                break
        except Exception:
            pass


def ler_telas(page, nome, url, run, lidos, hoje):
    chave = f"tela:{nome}:{hoje}"
    if chave in lidos:
        return []
    altura = int(page.evaluate("document.documentElement.scrollHeight"))
    if altura < 400:
        return []  # Página sem conteúdo real: não gasta cota da IA
    achados, completo = [], True
    for y in range(0, min(altura, 7200), 2400):
        h = min(2400, altura - y)
        png = page.screenshot(clip={"x": 0, "y": y, "width": VIEW_W, "height": h},
                              type="jpeg", quality=75)
        try:
            res = ler_com_ia([{"text": PROMPT}, parte_imagem("image/jpeg", png)], run)
        except Exception as e:
            log(f"  ! leitura do print: {str(e)[:150]}")
            res = None
        if res is None:
            completo = False
            break
        achados += res
    if completo:
        lidos[chave] = hoje
    return achados


def coletar_loja(page, loja, estado, run, hoje):
    nome, url = loja["nome"], loja["url"]
    lidos = estado["lidos"]
    resumo = {"imagens": 0, "imagens_novas": 0, "imagens_lidas": 0, "ofertas": 0}
    ofertas = []
    chamadas_antes = run["chamadas"]

    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PWTimeout:
        pass

    fechar_avisos_e_cookies(page)
    page.evaluate(JS_ROLAR)

    texto = page.inner_text("body")
    imagens = page.evaluate(JS_IMAGENS)
    if loja.get("excluir_contexto"):
        rx = re.compile(loja["excluir_contexto"], re.I)
        imagens = [i for i in imagens if not rx.search(i.get("ctx") or "")]
    resumo["imagens"] = len(imagens)
    log(f"  texto: {len(texto)} caracteres, {texto.count('R$')} símbolos R$; imagens candidatas: {len(imagens)}")

    # 1) Texto da página
    tem_texto = texto.count("R$") >= 5
    if tem_texto:
        chave = f"texto:{nome}:{hashlib.sha1(texto.encode('utf-8')).hexdigest()[:16]}"
        if chave not in lidos:
            try:
                res = ler_com_ia([{"text": PROMPT + "\n\nTexto da página:\n" + texto[:25000]}], run)
            except Exception as e:
                log(f"  ! leitura do texto: {str(e)[:150]}")
                res = None
            if res is not None:
                lidos[chave] = hoje
                ofertas += [(p, v, url) for p, v in res]

    # 2) Imagens de encarte
    novas = [i for i in imagens if "img:" + i["url"] not in lidos]
    resumo["imagens_novas"] = len(novas)
    baixadas = 0
    lote, lote_urls, bytes_lote = [], [], 0

    def enviar_lote():
        nonlocal lote, lote_urls, bytes_lote
        if not lote:
            return "ok"
        partes = [{"text": PROMPT_LOTE if len(lote) > 1 else PROMPT}] + lote
        urls_lote = lote_urls
        lote, lote_urls, bytes_lote = [], [], 0
        try:
            res = ler_com_ia(partes, run)
        except Exception as e:
            log(f"  ! leitura do lote: {str(e)[:150]}")
            return "falha"
        if res is None:
            return "parar"
        for u in urls_lote:
            lidos["img:" + u] = hoje
        resumo["imagens_lidas"] += len(urls_lote)
        ofertas.extend((p, v, url) for p, v in res)
        return "ok"

    for img in novas:
        if not lote and (run["parar"] or run["chamadas"] - chamadas_antes >= LIMITE_POR_LOJA):
            break
        try:
            baixada = baixar_imagem(page, img["url"], url)
        except Exception as e:
            log(f"  ! imagem {img['url'][-60:]}: {str(e)[:150]}")
            continue
        if baixada is None:
            lidos["img:" + img["url"]] = hoje
            continue
        baixadas += 1
        tipo, corpo = baixada
        if lote and bytes_lote + len(corpo) > 10 * 1024 * 1024:
            if enviar_lote() == "parar":
                break
        lote.append(parte_imagem(tipo, corpo))
        lote_urls.append(img["url"])
        bytes_lote += len(corpo)
        if len(lote) >= LOTE_IMAGENS and enviar_lote() == "parar":
            break
    enviar_lote()

    # 3) Plano B: prints da página (só se não achou texto nem imagens, e se a página tiver conteúdo)
    if not tem_texto and (not imagens or (novas and baixadas == 0)):
        if len(texto.strip()) > 300:  # Evita print de páginas vazias ou bloqueadas
            ofertas += [(p, v, url) for p, v in ler_telas(page, nome, url, run, lidos, hoje)]

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


def gravar_resultados(estado, novas, resumo, run, hoje):
    podar_estado(estado, hoje)
    (DADOS / "estado.json").write_text(json.dumps(estado, ensure_ascii=False, indent=1), "utf-8")
    gravar_csv(mesclar(ler_csv(), novas, hoje))
    (DADOS / "ultima-execucao.json").write_text(json.dumps({
        "quando": datetime.now(FUSO).isoformat(timespec="seconds"),
        "chamadas_ia": run["chamadas"],
        "limite_atingido": run["chamadas"] >= LIMITE_CHAMADAS,
        "parou_por_tempo": time.time() - run["inicio"] > TEMPO_MAX,
        "erro_ia": run["erro_ia"],
        "ofertas_novas": len(novas),
        "lojas": resumo,
    }, ensure_ascii=False, indent=1), "utf-8")


def main():
    if not os.environ.get("GEMINI_API_KEY"):
        log("ERRO: crie o segredo GEMINI_API_KEY no repositório (Settings > Secrets and variables > Actions).")
        return 1
    DADOS.mkdir(exist_ok=True)
    hoje = hoje_iso()
    estado = carregar_estado()
    run = {"chamadas": 0, "parar": False, "erro_ia": "", "inicio": time.time()}
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
                gravar_resultados(estado, novas, resumo, run, hoje)
        navegador.close()

    gravar_resultados(estado, novas, resumo, run, hoje)
    log(f"Concluído: {len(novas)} oferta(s) lida(s), {run['chamadas']} chamada(s) à IA.")

    if run["erro_ia"] and not novas:
        return 1
    if resumo and all("erro" in r for r in resumo.values()):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
