#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Coletor semanal do Cardápio do RU (UFG) + Notificação diária no WhatsApp (CallMeBot).

Fluxo:
  1. Acessa a página oficial da PRAE/UFG e pega o link .pdf mais recente do RU.
  2. Se for um PDF novo (diferente de `ultimo_pdf_url` no Firestore), baixa o PDF
     e usa o Gemini para extrair o cardápio da semana inteira em JSON.
  3. Salva o cardápio completo no Firestore (`casas/familia/settings/cardapio_ru`),
     que aparece na aba "Cardápio RU" do painel.
  4. Todo dia útil de manhã, olha APENAS o cardápio de hoje e compara com suas
     palavras-chave marcadas como ativas no painel. Se encontrar alguma, envia
     um aviso no seu WhatsApp via CallMeBot (no máximo 1 vez por dia).
"""
import base64
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

import firebase_admin
from firebase_admin import credentials, firestore

PRAE_URL = "https://prae.ufg.br/p/46332-cardapio-semanal-dos-restaurantes-universitarios"
FUSO = ZoneInfo("America/Sao_Paulo")
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

DIAS_SEMANA = [
    "Segunda-feira",
    "Terça-feira",
    "Quarta-feira",
    "Quinta-feira",
    "Sexta-feira",
    "Sábado",
    "Domingo",
]

PROMPT_RU = (
    "Você lê cardápios semanais em PDF dos Restaurantes Universitários (RU) da UFG. "
    "Extraia o cardápio completo da semana para a unidade solicitada (ou o cardápio geral caso seja único). "
    "Responda ESTRITAMENTE em JSON com o seguinte formato:\n"
    '{\n'
    '  "titulo": "Ex.: 3ª Semana - Outubro/2026",\n'
    '  "dias": [\n'
    '    {\n'
    '      "dia_semana": "Segunda-feira",\n'
    '      "data": "DD/MM",\n'
    '      "almoco": {\n'
    '        "principal": "prato principal",\n'
    '        "vegetariano": "opção vegetariana",\n'
    '        "guarnicao": "guarnição",\n'
    '        "acompanhamentos": "arroz, feijão, etc.",\n'
    '        "salada": "saladas",\n'
    '        "sobremesa": "sobremesa",\n'
    '        "suco": "suco"\n'
    '      },\n'
    '      "jantar": {\n'
    '        "principal": "...",\n'
    '        "vegetariano": "...",\n'
    '        "guarnicao": "...",\n'
    '        "acompanhamentos": "...",\n'
    '        "salada": "...",\n'
    '        "sobremesa": "...",\n'
    '        "suco": "..."\n'
    '      }\n'
    '    }\n'
    '  ]\n'
    '}\n'
    "Use exatamente os nomes dos dias: Segunda-feira, Terça-feira, Quarta-feira, Quinta-feira, Sexta-feira, Sábado. "
    "Se alguma refeição não existir no dia (ex.: jantar no sábado), deixe os campos vazios."
)


def log(*args):
    print(*args, flush=True)


def normalizar(s):
    s = unicodedata.normalize("NFD", str(s or "").lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn").strip()


def buscar_pdf_mais_recente():
    req = urllib.request.Request(PRAE_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as r:
        html = r.read().decode("utf-8", "replace")

    links = re.findall(r'href=["\']([^"\']+\.pdf[^"\']*)["\']', html, re.I)
    pdfs_ru = []
    for u in links:
        u_abs = urllib.parse.urljoin(PRAE_URL, u)
        nome = urllib.parse.unquote(u_abs).lower()
        if any(k in nome for k in ["ru_-_", "/ru_", "cardapio", "semana"]):
            if u_abs not in pdfs_ru:
                pdfs_ru.append(u_abs)

    if not pdfs_ru:
        return None
    # A PRAE lista as semanas em ordem cronológica na página; o último link é o mais recente
    return pdfs_ru[-1]


def chamar_gemini_pdf(pdf_bytes, unidade):
    chave = os.environ.get("GEMINI_API_KEY")
    if not chave:
        raise RuntimeError("GEMINI_API_KEY não configurada")

    b64 = base64.b64encode(pdf_bytes).decode("ascii")
    prompt = PROMPT_RU + f"\n\nUnidade de preferência: {unidade}."
    partes = [
        {"text": prompt},
        {"inline_data": {"mime_type": "application/pdf", "data": b64}},
    ]

    modelos = [os.environ.get("GEMINI_MODEL") or "gemini-3.8-flash", "gemini-3.5-flash-lite"]
    ultimo_erro = None

    for modelo in list(dict.fromkeys(modelos)):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent"
        gen_cfg = {"temperature": 0, "responseMimeType": "application/json"}
        if "lite" not in modelo.lower():
            gen_cfg["thinkingConfig"] = {"thinkingBudget": 0}

        for tentativa in range(3):
            corpo = json.dumps({
                "contents": [{"parts": partes}],
                "generationConfig": gen_cfg,
            }).encode("utf-8")
            req = urllib.request.Request(
                url, data=corpo,
                headers={"Content-Type": "application/json", "x-goog-api-key": chave},
            )
            try:
                with urllib.request.urlopen(req, timeout=90) as r:
                    dados = json.loads(r.read())
                texto = dados["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(texto)
            except urllib.error.HTTPError as e:
                detalhe = e.read()[:500]
                if e.code == 400 and "thinkingConfig" in gen_cfg:
                    del gen_cfg["thinkingConfig"]
                    continue
                if e.code in (503, 429) and tentativa < 2:
                    time.sleep((tentativa + 1) * 7)
                    continue
                ultimo_erro = f"HTTP {e.code}: {detalhe}"
                break
            except Exception as e:
                ultimo_erro = str(e)
                if tentativa < 2:
                    time.sleep(5)
                    continue
                break

    raise RuntimeError(f"Falha ao extrair PDF com Gemini: {ultimo_erro}")


def texto_refeicao(ref):
    if not isinstance(ref, dict):
        return str(ref or "")
    partes = [ref.get(k, "") for k in [
        "principal", "vegetariano", "guarnicao", "acompanhamentos", "salada", "sobremesa", "suco"
    ]]
    return " | ".join(str(p).strip() for p in partes if str(p or "").strip())


def notificar_whatsapp(mensagem):
    """Envia a mensagem para todos os números configurados (separados por vírgula)."""
    telefones = [t.strip() for t in (os.environ.get("WHATSAPP_PHONE") or "").split(",") if t.strip()]
    api_keys = [k.strip() for k in (os.environ.get("CALLMEBOT_API_KEY") or "").split(",") if k.strip()]
    if not telefones or not api_keys:
        log("! WHATSAPP_PHONE ou CALLMEBOT_API_KEY não configurados; pulando envio.")
        return False
    if len(telefones) != len(api_keys):
        log(f"! WHATSAPP_PHONE tem {len(telefones)} número(s) mas CALLMEBOT_API_KEY tem {len(api_keys)} chave(s). "
            "Separe por vírgula na mesma ordem. Enviando apenas para os pares válidos.")

    enviados = 0
    texto_cod = urllib.parse.quote(mensagem)
    for i, (tel, key) in enumerate(zip(telefones, api_keys)):
        url = f"https://api.callmebot.com/whatsapp.php?phone={tel}&text={texto_cod}&apikey={key}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as r:
                log(f"WhatsApp do RU enviado para {tel} (HTTP {r.status}).")
                enviados += 1
        except Exception as e:
            log(f"! Falha ao enviar WhatsApp para {tel}: {str(e)[:150]}")

    return enviados > 0


def main():
    cred_raw = os.environ.get("FIREBASE_CREDENTIALS")
    if not cred_raw:
        log("ERRO: segredo FIREBASE_CREDENTIALS não configurado.")
        return 1

    cred_dict = json.loads(cred_raw)
    firebase_admin.initialize_app(credentials.Certificate(cred_dict))
    db = firestore.client()

    doc_ref = db.collection("casas").document("familia").collection("settings").document("cardapio_ru")
    snap = doc_ref.get()
    dados = snap.to_dict() if snap.exists else {}

    unidade = dados.get("unidade") or "Colemar Natal e Silva"
    ultimo_pdf = dados.get("ultimo_pdf_url") or ""

    # 1) Verifica se saiu um PDF novo na página da PRAE
    pdf_url = buscar_pdf_mais_recente()
    if not pdf_url:
        log("! Nenhum PDF do RU encontrado na página da PRAE.")
    elif pdf_url != ultimo_pdf or not dados.get("dias"):
        log(f"Novo PDF encontrado: {pdf_url}")
        req = urllib.request.Request(pdf_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=45) as r:
            pdf_bytes = r.read()
        extraido = chamar_gemini_pdf(pdf_bytes, unidade)
        dias = extraido.get("dias") or []
        titulo = extraido.get("titulo") or "Cardápio da Semana"

        doc_ref.set({
            "unidade": unidade,
            "titulo": titulo,
            "dias": dias,
            "ultimo_pdf_url": pdf_url,
            "fonte_pagina_url": PRAE_URL,
            "atualizado_em": datetime.now(FUSO).isoformat(timespec="seconds"),
        }, merge=True)
        dados["dias"] = dias
        dados["titulo"] = titulo
        dados["ultimo_pdf_url"] = pdf_url
        log(f"Cardápio salvo no Firestore ({len(dias)} dias).")
    else:
        log("PDF da semana já está atualizado no Firestore. Economizando chamada de IA.")

    # 2) Verifica se HOJE tem algum prato favorito marcado como ativo
    agora = datetime.now(FUSO)
    if agora.weekday() == 6:
        log("Domingo: RU fechado, nenhum aviso a enviar hoje.")
        return 0
    hoje_str = agora.date().isoformat()
    dia_hoje = DIAS_SEMANA[agora.weekday()]
    data_ddmm = agora.strftime("%d/%m")

    if dados.get("ultimo_aviso_data") == hoje_str:
        log("Aviso de hoje já foi enviado anteriormente.")
        return 0

    palavras = [
        p.get("termo", "").strip()
        for p in (dados.get("palavras_chave") or [])
        if isinstance(p, dict) and p.get("ativo", True) and p.get("termo", "").strip()
    ]
    if not palavras:
        log("Nenhuma palavra-chave ativa configurada no painel.")
        return 0

    dias = dados.get("dias") or []
    dia_obj = None
    for d in dias:
        if normalizar(d.get("dia_semana")) == normalizar(dia_hoje) or str(d.get("data", "")).strip() == data_ddmm:
            dia_obj = d
            break

    if not dia_obj:
        log(f"Nenhum cardápio encontrado para hoje ({dia_hoje} {data_ddmm}).")
        return 0

    achados = []
    for refeicao_key, label in [("almoco", "Almoço"), ("jantar", "Jantar")]:
        txt = texto_refeicao(dia_obj.get(refeicao_key))
        txt_norm = normalizar(txt)
        if not txt_norm:
            continue
        termos_batidos = [t for t in palavras if normalizar(t) in txt_norm]
        if termos_batidos:
            principal = ""
            if isinstance(dia_obj.get(refeicao_key), dict):
                principal = dia_obj[refeicao_key].get("principal") or ""
            achados.append(f"• *{label}:* {', '.join(termos_batidos)}" + (f" ({principal})" if principal else ""))

    if not achados:
        log(f"Hoje ({dia_hoje}) não tem nenhum dos itens marcados.")
        return 0

    msg = (
        f"🍽️ *RU UFG ({unidade}) — {dia_hoje} ({data_ddmm})*\n\n"
        f"Hoje tem item(ns) da sua lista:\n"
        + "\n".join(achados)
    )
    if notificar_whatsapp(msg):
        doc_ref.set({"ultimo_aviso_data": hoje_str}, merge=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
