#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Coletor de ofertas dos supermercados (roda no GitHub Actions, de graça).

Melhorias definitivas para os 4 supermercados:
  1. Auto-recuperação: se um mercado ainda não tiver ofertas no ofertas.csv,
     ignora o cache antigo (estado.json) daquele mercado automaticamente.
  2. Tatico Goiânia: busca os encartes reais em /gyn/tabloide/, /gyn/ofertas/
     e na API WordPress (/gyn/wp-json/wp/v2/media), usando proxy do Google/Cloudflare
     (translate.goog e wsrv.nl) quando o IP do GitHub Actions (EUA) for bloqueado.
  3. Assaí Independência: extrai todos os encartes diretamente do JSON interno
     do Assaí (Drupal.settings.assai.ofertasJson) + carrossel e prioriza o
     download HTTP em alta resolução do CloudFront antes de qualquer print.
  4. Filtro inteligente: não descarta ofertas por ano de validade inferido pela IA,
     ignora miniaturas 250x250 sem preço do Pro Brazilian e filtra encartes de
     outras cidades (DF/Entorno/Trindade) na URL do Costa Atacadão.
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
import urllib.parse
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
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

LOJAS = [
    {
        "nome": "Tatico",
        "url": "https://taticobaratotododia.com.br/gyn/tabloide/",
        "urls_extras": [
            "https://taticobaratotododia.com.br/gyn/ofertas/",
        ],
    },
    {"nome": "Pro Brazilian", "url": "https://www.probrazilian.com.br/ofertas"},
    {"nome": "Assaí", "url": "https://www.assai.com.br/ofertas/goias/assai-independencia"},
    {
        "nome": "Costa Atacadão",
        "url": "https://costaatacadao.com.br/ofertas/",
        "excluir_contexto": r"Distrito Federal|Valpara|Rio Verde|Luzi[aâ]nia|Senador Canedo|Trindade",
        "excluir_url": r"[-_](df|entorno)[-_.]|trindade|rio-verde|valparaiso|luziania|senador-canedo",
    },
]

LIMITE_CHAMADAS = int(os.environ.get("LIMITE_CHAMADAS") or 30)
PAUSA = float(os.environ.get("PAUSA_ENTRE_CHAMADAS") or 2)
LIMITE_POR_LOJA = int(os.environ.get("LIMITE_POR_LOJA") or 6)
LOTE_IMAGENS = int(os.environ.get("LOTE_IMAGENS") or 3)
TEMPO_MAX = float(os.environ.get("TEMPO_MAXIMO_MIN") or 20) * 60
DIAS_HISTORICO = 60

PROMPT = (
    "Você lê ofertas de supermercado do Brasil. Extraia TODOS os produtos que tenham preço visível. "
    "Responda somente com um array JSON; cada item no formato "
    '{"produto": "nome com marca e tamanho quando aparecerem (ex.: Arroz Branco Tio João 5kg)", '
    '"preco": número em reais (ex.: 26.9)}. '
    "Regras: use o preço da embalagem/unidade como anunciado (só use preço por kg quando for o único); "
    "ignore itens sem preço legível; não invente nada; sem texto fora do JSON; se não houver ofertas, responda []."
)

PROMPT_LOTE = PROMPT + (" Vários encartes ou páginas do mesmo supermercado seguem anexados; "
                        "leia todos e junte os produtos num único array.")

JS_IMAGENS = r"""
() => {
  const BAD = /logo|icon|favicon|sprite|avatar|pixel|loading|placeholder|feed\.png|maskot-404|sol-celular|banner-passai|google-play|app-store|ico-|bg-sac|bg-sobre|bg-portal|bg-t7|bg-centro|bg-papilon|bg-campinas|bg-chamada|bg-garavelo|bg-top|bg-6-lojas|img-email|img-whatsapp|img-lojas|BANNER-VAGAS|BANNER-BEM-VINDO|produto-assets-vipcommerce|\/uploads\/202[0-4]\/|\/(styles|themes|icons|assets|sprites)\//i;
  const THUMB = /(-\d{2,4}x\d{2,4}\.(jpe?g|png|webp))|(\/\d{2,3}x\d{2,3}\/)/i;
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

  // 1) Suporte especial Assaí: extrai todos os encartes da loja direto do JSON do Drupal
  try {
    const bloco = document.querySelector('.bloco-ofertas-tabloide');
    const nid = bloco ? Number(bloco.getAttribute('data-nid') || 2562) : 2562;
    const eid = bloco ? Number(bloco.getAttribute('data-eid') || 8) : 8;
    const assai = window.Drupal && window.Drupal.settings && window.Drupal.settings.assai;
    if (assai) {
      const listas = [assai.ofertasJson || [], assai.ofertasJson_v1 || []];
      for (const lista of listas) {
        for (const ofr of (lista || [])) {
          const lojas = ofr.lojas || [];
          const pertence = !nid || lojas.some(l => Number(l.nid) === nid && (!eid || Number(l.eid) === eid));
          if (pertence && Array.isArray(ofr.images)) {
            for (const im of ofr.images) {
              const u = typeof im === 'string' ? im : (im && im.url) || '';
              if (ehImg(u) && !BAD.test(u) && !achados.has(u)) {
                achados.set(u, ofr.title || 'Encarte Assaí');
              }
            }
          }
        }
      }
    }
  } catch (e) {}

  // 2) Imagens e links do DOM
  document.querySelectorAll('img').forEach(img => {
    if (img.closest('header, footer, nav')) return;
    const a = img.closest('a');
    let url = (a && ehImg(a.href)) ? a.href : pegarUrl(img);

    if ((!url || BAD.test(url)) && img.srcset) {
      const partes = img.srcset.split(',').map(s => s.trim().split(' ')[0]);
      url = partes[partes.length - 1] || url;
    }

    if (!ehImg(url) || BAD.test(url) || THUMB.test(url) || achados.has(url)) return;
    achados.set(url, contexto(a || img));
  });

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
  for (let i = 0; i < 25; i++) {
    const el = document.scrollingElement || document.documentElement || document.body;
    if (!el) break;
    window.scrollBy(0, 800);
    await esperar(200);
    if (window.innerHeight + window.scrollY >= el.scrollHeight - 10) break;
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

    modelo_preferido = os.environ.get("GEMINI_MODEL") or "gemini-3.8-flash"
    candidatos = [modelo_preferido, "gemini-3.5-flash-lite"]
    modelos = list(dict.fromkeys(candidatos))

    ultimo_erro = None

    for modelo in modelos:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent"

        generation_config = {
            "temperature": 0,
            "responseMimeType": "application/json",
        }
        if "lite" not in modelo.lower():
            generation_config["thinkingConfig"] = {"thinkingBudget": 0}

        for tentativa in range(3):
            corpo = json.dumps({
                "contents": [{"parts": partes}],
                "generationConfig": generation_config,
            }).encode("utf-8")

            req = urllib.request.Request(
                url, data=corpo,
                headers={"Content-Type": "application/json", "x-goog-api-key": chave},
            )
            try:
                with urllib.request.urlopen(req, timeout=75) as r:
                    dados = json.loads(r.read())
                texto = dados["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(texto)
            except urllib.error.HTTPError as e:
                detalhe = e.read()[:1500]
                if e.code == 400 and "thinkingConfig" in generation_config:
                    del generation_config["thinkingConfig"]
                    continue
                if e.code == 503:
                    if tentativa < 2:
                        espera = (tentativa + 1) * 7
                        log(f"  ! {modelo} com alta demanda (HTTP 503). Aguardando {espera}s...")
                        time.sleep(espera)
                        continue
                    else:
                        log(f"  ! {modelo} continuou em alta demanda. Alternando modelo...")
                        break
                if e.code == 429:
                    if b"per day" in detalhe.lower() or b"perday" in detalhe.lower():
                        log(f"  ! Cota diária atingida em {modelo}. Alternando modelo...")
                        break
                    time.sleep(12)
                    continue
                if e.code in (400, 401, 403, 404):
                    texto_erro = detalhe.decode("utf-8", "replace").replace("\n", " ")
                    raise ErroFatalIA(f"Gemini HTTP {e.code} ({modelo}): {texto_erro}")
                ultimo_erro = f"HTTP {e.code}: {detalhe!r}"
            except (TimeoutError, urllib.error.URLError, ConnectionError) as e:
                if tentativa < 2:
                    time.sleep(5)
                    continue
                ultimo_erro = str(e)

    raise CotaEsgotada(f"Modelos da IA indisponíveis temporariamente: {ultimo_erro}")


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
        run["erros_consecutivos"] = 0
        log(f"  IA #{run['chamadas']}: {len(res)} oferta(s) em {time.time() - t0:.0f}s")
        return res
    except (CotaEsgotada, ErroFatalIA) as e:
        run["erros_consecutivos"] = run.get("erros_consecutivos", 0) + 1
        log("  ! Aviso na IA: " + str(e)[:250])
        if run["erros_consecutivos"] >= 5:
            run["parar"] = True
            log("  ! 5 erros consecutivos na IA. Interrompendo coleta geral.")
        return None


# ---------------------------------------------------------------- páginas e imagens
def tipo_da_imagem(headers, url):
    tipo = (headers.get("content-type") or "").split(";")[0].strip().lower()
    if tipo not in ("image/jpeg", "image/png", "image/webp"):
        ext = url.split("?")[0].rsplit(".", 1)[-1].lower()
        tipo = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(ext)
    return tipo or "image/jpeg"


def validar_imagem(tipo, corpo):
    if not corpo or len(corpo) < 8000 or len(corpo) > 15 * 1024 * 1024:
        return None
    # Verifica assinatura mágica básica (JPEG, PNG, WEBP) para não enviar HTML de erro à IA
    if corpo[:2] == b"\xff\xd8":
        return "image/jpeg", corpo
    if corpo[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png", corpo
    if corpo[:4] == b"RIFF" and corpo[8:12] == b"WEBP":
        return "image/webp", corpo
    return None


def baixar_via_urllib(url, referer):
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Referer": referer,
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=25) as r:
        return validar_imagem(tipo_da_imagem(r.headers, url), r.read())


def baixar_imagem(page, url, referer, cache_rede):
    # 1) Verifica se a imagem já foi capturada no tráfego de rede do navegador
    nome_arquivo = url.split("?")[0].split("/")[-1]
    for u, (ct, bytes_img) in cache_rede.items():
        if nome_arquivo and nome_arquivo in u:
            ok = validar_imagem(ct, bytes_img)
            if ok:
                return ok

    # 2) Download HTTP direto com urllib (ideal para CloudFront do Assaí e WP do Costa)
    try:
        ok = baixar_via_urllib(url, referer)
        if ok:
            return ok
    except Exception:
        pass

    # 3) Download via contexto de rede do Playwright
    try:
        r = page.request.get(url, timeout=25000, headers={
            "Referer": referer,
            "User-Agent": USER_AGENT,
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        })
        if r.ok:
            ok = validar_imagem(tipo_da_imagem(r.headers, url), r.body())
            if ok:
                return ok
    except Exception:
        pass

    # 4) Fallback via proxies globais (resolve bloqueio de IP dos EUA no Tatico)
    urls_proxy = [
        "https://wsrv.nl/?url=" + urllib.parse.quote(url, safe=""),
    ]
    if "taticobaratotododia.com.br/" in url:
        urls_proxy.insert(
            0,
            url.replace("https://taticobaratotododia.com.br/",
                        "https://taticobaratotododia-com-br.translate.goog/")
            + ("&_x_tr_sl=auto&_x_tr_tl=pt" if "?" in url else "?_x_tr_sl=auto&_x_tr_tl=pt")
        )
    for up in urls_proxy:
        try:
            ok = baixar_via_urllib(up, referer)
            if ok:
                return ok
        except Exception:
            pass

    # 5) Último recurso: screenshot apenas da tag <img> visível no DOM
    try:
        for p in [page] + page.frames:
            try:
                loc = p.locator(f'img[src*="{nome_arquivo}"]').first
                if loc.count() > 0 and loc.is_visible():
                    loc.scroll_into_view_if_needed(timeout=2000)
                    ok = validar_imagem("image/jpeg", loc.screenshot(type="jpeg", quality=85))
                    if ok:
                        return ok
            except Exception:
                pass
    except Exception:
        pass

    return None


def descobrir_imagens_tatico():
    """Busca os encartes atuais do Tatico Goiânia mesmo quando o IP dos EUA é bloqueado."""
    achados = []
    vistos = set()

    def registrar(u):
        u = u.split("?")[0].replace("https://taticobaratotododia-com-br.translate.goog/",
                                    "https://taticobaratotododia.com.br/")
        if not re.search(r"/uploads/202[5-9]/\d{2}/[^\"'<>]+\.(?:jpe?g|png|webp)$", u, re.I):
            return
        if re.search(r"-\d{2,4}x\d{2,4}\.", u):
            return
        if re.search(r"BANNER-VAGAS|BANNER-BEM-VINDO|BANNER-TATICO|BANNER-1000|BaB_Ype", u, re.I):
            return
        if u not in vistos:
            vistos.add(u)
            achados.append({"url": u, "ctx": "Encarte Tatico Goiânia"})

    # 1) Páginas de Tabloide e Ofertas (direto + via Google Translate proxy)
    fontes_html = [
        "https://taticobaratotododia.com.br/gyn/tabloide/",
        "https://taticobaratotododia.com.br/gyn/ofertas/",
        "https://taticobaratotododia-com-br.translate.goog/gyn/tabloide/?_x_tr_sl=auto&_x_tr_tl=pt&_x_tr_hl=pt-BR",
        "https://taticobaratotododia-com-br.translate.goog/gyn/ofertas/?_x_tr_sl=auto&_x_tr_tl=pt&_x_tr_hl=pt-BR",
    ]
    for u in fontes_html:
        try:
            req = urllib.request.Request(u, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=20) as r:
                html = r.read().decode("utf-8", "replace")
            for m in re.findall(r"https?://[a-zA-Z0-9./_~:-]+/uploads/202[5-9]/[0-9]{2}/[a-zA-Z0-9._-]+\.(?:jpe?g|png|webp)", html, re.I):
                registrar(m)
        except Exception:
            pass

    # 2) API REST de Mídia do WordPress (direto + via Google Translate proxy)
    fontes_api = [
        "https://taticobaratotododia.com.br/gyn/wp-json/wp/v2/media?per_page=20",
        "https://taticobaratotododia-com-br.translate.goog/gyn/wp-json/wp/v2/media?per_page=20&_x_tr_sl=auto&_x_tr_tl=pt&_x_tr_hl=pt-BR",
    ]
    for u in fontes_api:
        if achados:
            break
        try:
            req = urllib.request.Request(u, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=20) as r:
                itens = json.loads(r.read().decode("utf-8", "replace"))
            if isinstance(itens, list):
                for it in itens:
                    if isinstance(it, dict) and it.get("source_url"):
                        registrar(str(it["source_url"]))
        except Exception:
            pass

    return achados


def parte_imagem(tipo, corpo):
    return {"inline_data": {"mime_type": tipo, "data": base64.b64encode(corpo).decode("ascii")}}


def fechar_avisos_e_cookies(page):
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
    try:
        png = page.screenshot(type="jpeg", quality=75)
        res = ler_com_ia([{"text": PROMPT}, parte_imagem("image/jpeg", png)], run)
        if res:
            lidos[chave] = hoje
            return res
    except Exception as e:
        log(f"  ! leitura do print: {str(e)[:150]}")
    return []


def coletar_loja(page, loja, estado, run, hoje, forcar_releitura=False):
    nome, url = loja["nome"], loja["url"]
    lidos = estado["lidos"]
    resumo = {"imagens": 0, "imagens_novas": 0, "imagens_lidas": 0, "ofertas": 0}
    ofertas = []
    chamadas_antes = run["chamadas"]

    if forcar_releitura:
        log(f"  * {nome} ainda está sem ofertas no CSV; forçando leitura completa dos encartes...")

    cache_rede = {}
    def salvar_resposta(res):
        try:
            u = res.url
            if res.status == 200 and any(ext in u.lower() for ext in [".jpg", ".jpeg", ".png", ".webp"]):
                ct = res.headers.get("content-type") or "image/jpeg"
                corpo = res.body()
                if len(corpo) >= 8000:
                    cache_rede[u] = (ct, corpo)
        except Exception:
            pass
    page.on("response", salvar_resposta)

    texto = ""
    imagens = []

    urls_visitar = [url] + list(loja.get("urls_extras") or [])
    for u_pag in urls_visitar:
        try:
            page.goto(u_pag, wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except PWTimeout:
                pass

            # No Assaí, aguarda o JSON de encartes (ofertas_assai.json) carregar no Drupal
            if "assai.com.br" in u_pag:
                try:
                    page.wait_for_function(
                        "() => (window.Drupal && window.Drupal.settings && window.Drupal.settings.assai && "
                        "window.Drupal.settings.assai.ofertasJson && window.Drupal.settings.assai.ofertasJson.length > 0) || "
                        "document.querySelectorAll('.ofertas-slider img').length > 0",
                        timeout=15000
                    )
                    page.wait_for_timeout(1000)
                except Exception:
                    pass

            fechar_avisos_e_cookies(page)
            page.evaluate(JS_ROLAR)

            t_pag = page.inner_text("body")
            if t_pag:
                texto += "\n" + t_pag
            imgs_pag = page.evaluate(JS_IMAGENS)
            if imgs_pag:
                imagens.extend(imgs_pag)

            for frame in page.frames:
                if frame != page.main_frame:
                    try:
                        imgs_frame = frame.evaluate(JS_IMAGENS)
                        if imgs_frame:
                            imagens.extend(imgs_frame)
                        t_frame = frame.inner_text("body")
                        if t_frame and len(t_frame) > 100:
                            texto += "\n" + t_frame
                    except Exception:
                        pass
        except Exception as e:
            log(f"  ! Aviso ao abrir {u_pag}: {str(e)[:120]}")

    # Se for Tatico e o acesso direto não trouxe encartes de 2025+, busca via descoberta direta/proxy
    if nome == "Tatico":
        imagens = [i for i in imagens if re.search(r"/uploads/202[5-9]/", i.get("url") or "")]
        if not imagens:
            log("  ! Buscando encartes do Tatico Goiânia via rota alternativa (anti-bloqueio)...")
            imagens = descobrir_imagens_tatico()

    # Desduplica imagens mantendo a ordem
    vistos_urls = set()
    imagens_unicas = []
    for i in imagens:
        u = i.get("url")
        if u and u not in vistos_urls:
            vistos_urls.add(u)
            imagens_unicas.append(i)
    imagens = imagens_unicas

    if loja.get("excluir_contexto"):
        rx = re.compile(loja["excluir_contexto"], re.I)
        imagens = [i for i in imagens if not rx.search(i.get("ctx") or "")]
    if loja.get("excluir_url"):
        rx_u = re.compile(loja["excluir_url"], re.I)
        imagens = [i for i in imagens if not rx_u.search(urllib.parse.unquote(i.get("url") or ""))]

    resumo["imagens"] = len(imagens)
    log(f"  texto: {len(texto)} caracteres, {texto.count('R$')} símbolos R$; imagens candidatas: {len(imagens)}")

    # 1) Texto da página
    tem_texto = texto.count("R$") >= 5
    if tem_texto:
        chave = f"texto:{nome}:{hashlib.sha1(texto.encode('utf-8')).hexdigest()[:16]}"
        if forcar_releitura or chave not in lidos:
            try:
                res = ler_com_ia([{"text": PROMPT + "\n\nTexto da página:\n" + texto[:25000]}], run)
            except Exception as e:
                log(f"  ! leitura do texto: {str(e)[:150]}")
                res = None
            if res is not None:
                if res:
                    lidos[chave] = hoje
                ofertas += [(p, v, url) for p, v in res]

    # 2) Imagens de encarte
    if forcar_releitura:
        novas = list(imagens)
    else:
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
            return "falha"
        if res:
            for u in urls_lote:
                lidos["img:" + u] = hoje
        resumo["imagens_lidas"] += len(urls_lote)
        ofertas.extend((p, v, url) for p, v in res)
        return "ok"

    for img in novas:
        if not lote and (run["parar"] or run["chamadas"] - chamadas_antes >= LIMITE_POR_LOJA):
            break
        try:
            baixada = baixar_imagem(page, img["url"], url, cache_rede)
        except Exception as e:
            log(f"  ! imagem {img['url'][-60:]}: {str(e)[:150]}")
            continue
        if baixada is None:
            continue
        baixadas += 1
        tipo, corpo = baixada
        if lote and bytes_lote + len(corpo) > 10 * 1024 * 1024:
            enviar_lote()
        lote.append(parte_imagem(tipo, corpo))
        lote_urls.append(img["url"])
        bytes_lote += len(corpo)
        if len(lote) >= LOTE_IMAGENS:
            enviar_lote()
    enviar_lote()

    # 3) Plano B: prints da página
    if not ofertas and not tem_texto and (not imagens or (novas and baixadas == 0)):
        if len(texto.strip()) > 300:
            ofertas += [(p, v, url) for p, v in ler_telas(page, nome, url, run, lidos, hoje)]

    resumo["ofertas"] = len(ofertas)
    return resumo, ofertas


# ---------------------------------------------------------------- arquivos
def carregar_estado():
    try:
        e = json.loads((DADOS / "estado.json").read_text("utf-8"))
        if isinstance(e.get("lidos"), dict):
            # Limpa entradas antigas inválidas (404, ícones ou miniaturas sem preço)
            lixo = re.compile(r"maskot-404|feed\.png|produto-assets-vipcommerce|/250x250/", re.I)
            e["lidos"] = {k: v for k, v in e["lidos"].items() if not lixo.search(k)}
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
        "erro_ia": run.get("erro_ia", ""),
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
    existentes = ler_csv()
    mercados_com_oferta = {l.get("supermercado") for l in existentes if l.get("supermercado")}

    run = {"chamadas": 0, "parar": False, "erro_ia": "", "erros_consecutivos": 0, "inicio": time.time()}
    novas, resumo = [], {}

    with sync_playwright() as pw:
        navegador = pw.chromium.launch(
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"]
        )
        contexto = navegador.new_context(
            locale="pt-BR", timezone_id="America/Sao_Paulo",
            viewport={"width": VIEW_W, "height": 900}, user_agent=USER_AGENT,
            extra_http_headers={"Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7"}
        )
        contexto.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        for loja in LOJAS:
            log(f"== {loja['nome']} ({loja['url']})")
            page = contexto.new_page()
            forcar = loja["nome"] not in mercados_com_oferta
            try:
                resumo[loja["nome"]], ofertas = coletar_loja(
                    page, loja, estado, run, hoje, forcar_releitura=forcar
                )
                for produto, preco, link in ofertas:
                    novas.append({"data": hoje, "supermercado": loja["nome"], "produto": produto,
                                  "preco": f"{preco:.2f}", "link": link})
                    mercados_com_oferta.add(loja["nome"])
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

    if run.get("erro_ia") and not novas:
        return 1
    if resumo and all("erro" in r for r in resumo.values()):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
