#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Coletor do Cardápio do RU (UFG) via API + Notificação diária no WhatsApp.

Fluxo (sem Gemini, sem PDF — tudo estruturado via API):
  1. Baixa o bundle JS do app RU UFG (ru-ufg.web.ufg.br) e extrai as
     credenciais OAuth2 de serviço (públicas, embutidas no frontend).
  2. Obtém um Bearer token via Keycloak (quimera.api.ufg.br).
  3. Consulta a API estruturada do cardápio (cerberus.api.ufg.br)
     para almoço (horario=0) e jantar (horario=1) da semana inteira.
  4. Salva o cardápio no Firestore (casas/familia/settings/cardapio_ru).
  5. Compara o cardápio de hoje com as palavras-chave ativas do usuário
     e envia WhatsApp via CallMeBot quando encontrar algum prato favorito.
"""
import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

import firebase_admin
from firebase_admin import credentials, firestore

RU_WEB_URL = "https://ru-ufg.web.ufg.br"
CARDAPIO_API = "https://cerberus.api.ufg.br/api/cardapio-ru/cardapios"
FUSO = ZoneInfo("America/Sao_Paulo")
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

DIAS_SEMANA = [
    "Segunda-feira", "Terça-feira", "Quarta-feira",
    "Quinta-feira", "Sexta-feira", "Sábado", "Domingo",
]


def log(*args):
    print(*args, flush=True)


def normalizar(s):
    s = unicodedata.normalize("NFD", str(s or "").lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn").strip()


# ─── Autenticação via service account do app (credenciais públicas no JS) ───

def extrair_credenciais_app():
    """Baixa o bundle JS do app RU UFG e extrai as credenciais OAuth2."""
    req = urllib.request.Request(RU_WEB_URL + "/", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as r:
        html = r.read().decode("utf-8", "replace")

    m = re.search(r'src="(main\.[a-f0-9]+\.js)"', html)
    if not m:
        raise RuntimeError("Não encontrou o bundle main.*.js no HTML do RU UFG")

    js_url = f"{RU_WEB_URL}/{m.group(1)}"
    log(f"  Bundle JS: {m.group(1)}")
    req = urllib.request.Request(js_url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as r:
        js = r.read().decode("utf-8", "replace")

    def extrair(chave):
        p = re.search(rf'{chave}\s*:\s*"([^"]+)"', js)
        if not p:
            p = re.search(rf"{chave}\s*:\s*'([^']+)'", js)
        if not p:
            raise RuntimeError(f"Não encontrou {chave} no bundle JS")
        return p.group(1)

    return {
        "gw_url": extrair("gw_url"),
        "gw_realm": extrair("gw_healm"),
        "gw_client_id": extrair("gw_client_id"),
        "gw_client_secret": extrair("gw_client_secret"),
        "gw_user": extrair("gw_user"),
        "gw_passwd": extrair("gw_passwd"),
    }


def obter_token(creds):
    """Obtém um Bearer token via Keycloak usando as credenciais do app."""
    url = (f"{creds['gw_url']}/auth/realms/{creds['gw_realm']}"
           "/protocol/openid-connect/token")
    body = urllib.parse.urlencode({
        "client_id": creds["gw_client_id"],
        "client_secret": creds["gw_client_secret"],
        "grant_type": "password",
        "scope": "email roles profile",
        "username": creds["gw_user"],
        "password": creds["gw_passwd"],
    }).encode("utf-8")

    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        dados = json.loads(r.read())
    return dados["access_token"]


# ─── Consulta da API do Cardápio ────────────────────────────────────────────

def buscar_cardapio_semanal(token, horario):
    """Busca o cardápio semanal para um tipo de refeição (0=almoço, 1=jantar)."""
    url = f"{CARDAPIO_API}/cardapio-semanal?horario={horario}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def mapear_categorias(categorias):
    """Mapeia as categorias da API para o formato do frontend."""
    resultado = {}
    arroz = ""
    feijao = ""

    for cat in (categorias or []):
        nome_norm = normalizar(cat.get("nome", ""))
        itens = [it.get("nome", "").strip() for it in (cat.get("itens") or [])
                 if it.get("nome", "").strip()]
        texto = " / ".join(itens)
        if not texto:
            continue

        if "proteico" in nome_norm:
            resultado["principal"] = texto
        elif "ovolacto" in nome_norm:
            resultado["vegetariano"] = texto
        elif "vegana" in nome_norm or "vegano" in nome_norm:
            resultado["vegano"] = texto
        elif "guarnicao" in nome_norm:
            resultado["guarnicao"] = texto
        elif "arroz" in nome_norm:
            arroz = texto
        elif "feijao" in nome_norm:
            feijao = texto
        elif "salada" in nome_norm:
            resultado["salada"] = texto
        elif "refresco" in nome_norm:
            resultado["suco"] = texto
        elif "sobremesa" in nome_norm:
            resultado["sobremesa"] = texto

    acomp = " / ".join(p for p in [arroz, feijao] if p)
    if acomp:
        resultado["acompanhamentos"] = acomp

    return resultado


def resolver_data(valor):
    """Converte o campo data da API para dd/MM (pode ser timestamp ms, ISO string, etc.)."""
    if isinstance(valor, (int, float)):
        try:
            # Timestamps em ms (Java/JS style)
            if valor > 1e12:
                valor = valor / 1000
            dt = datetime.fromtimestamp(valor, tz=FUSO)
            return dt.strftime("%d/%m")
        except Exception:
            return str(valor)
    s = str(valor or "").strip()
    # Se vier como ISO "2026-11-19T..." ou "2026-11-19", extrair dd/MM
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m.group(3)}/{m.group(2)}"
    return s


def resolver_dia_semana(valor):
    """Converte o campo diaSemana da API (pode ser int ou string) para nome legível."""
    if isinstance(valor, int):
        if 0 <= valor < len(DIAS_SEMANA):
            return DIAS_SEMANA[valor]
        return f"Dia {valor}"
    return str(valor or "").strip()


def montar_semana(almoco_api, jantar_api):
    """Combina as respostas de almoço e jantar num único array de dias."""
    dias_map = {}

    for refeicao_key, dados_api in [("almoco", almoco_api), ("jantar", jantar_api)]:
        if dados_api:
            log(f"  API {refeicao_key}: {len(dados_api)} dia(s). "
                f"Exemplo diaSemana={dados_api[0].get('diaSemana')!r} "
                f"({type(dados_api[0].get('diaSemana')).__name__})")
        for dia in (dados_api or []):
            dia_semana = resolver_dia_semana(dia.get("diaSemana"))
            data_raw = resolver_data(dia.get("data"))
            if not dia_semana:
                continue

            if dia_semana not in dias_map:
                dias_map[dia_semana] = {
                    "dia_semana": dia_semana,
                    "data": data_raw,
                }

            categorias = dia.get("categorias")
            if categorias:
                dias_map[dia_semana][refeicao_key] = mapear_categorias(categorias)
            else:
                dias_map[dia_semana][refeicao_key] = {}

    ordem = {d: i for i, d in enumerate(DIAS_SEMANA)}
    return sorted(dias_map.values(), key=lambda d: ordem.get(d["dia_semana"], 99))


# ─── WhatsApp via CallMeBot ─────────────────────────────────────────────────

def notificar_whatsapp(mensagem):
    """Envia a mensagem para todos os números configurados (separados por vírgula)."""
    telefones = [t.strip() for t in (os.environ.get("WHATSAPP_PHONE") or "").split(",") if t.strip()]
    api_keys = [k.strip() for k in (os.environ.get("CALLMEBOT_API_KEY") or "").split(",") if k.strip()]
    if not telefones or not api_keys:
        log("! WHATSAPP_PHONE ou CALLMEBOT_API_KEY não configurados; pulando envio.")
        return False
    if len(telefones) != len(api_keys):
        log(f"! WHATSAPP_PHONE tem {len(telefones)} número(s) mas CALLMEBOT_API_KEY tem "
            f"{len(api_keys)} chave(s). Enviando apenas para os pares válidos.")

    enviados = 0
    texto_cod = urllib.parse.quote(mensagem)
    for tel, key in zip(telefones, api_keys):
        url = f"https://api.callmebot.com/whatsapp.php?phone={tel}&text={texto_cod}&apikey={key}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as r:
                log(f"WhatsApp do RU enviado para {tel} (HTTP {r.status}).")
                enviados += 1
        except Exception as e:
            log(f"! Falha ao enviar WhatsApp para {tel}: {str(e)[:150]}")

    return enviados > 0


# ─── Helpers para texto ─────────────────────────────────────────────────────

def texto_refeicao(ref):
    if not isinstance(ref, dict):
        return str(ref or "")
    return " | ".join(str(v).strip() for v in ref.values() if str(v or "").strip())


# ─── Main ───────────────────────────────────────────────────────────────────

def main():
    cred_raw = os.environ.get("FIREBASE_CREDENTIALS")
    if not cred_raw:
        log("ERRO: segredo FIREBASE_CREDENTIALS não configurado.")
        return 1

    cred_dict = json.loads(cred_raw)
    if not firebase_admin._apps:
        firebase_admin.initialize_app(credentials.Certificate(cred_dict))
    db = firestore.client()

    doc_ref = (db.collection("casas").document("familia")
               .collection("settings").document("cardapio_ru"))
    snap = doc_ref.get()
    dados = snap.to_dict() if snap.exists else {}

    agora = datetime.now(FUSO)
    hoje_str = agora.date().isoformat()

    # 1) Autenticar na API do RU UFG
    log("Extraindo credenciais do app RU UFG...")
    try:
        creds = extrair_credenciais_app()
    except Exception as e:
        log(f"ERRO ao extrair credenciais: {e}")
        return 1

    log("Obtendo token OAuth2 via Keycloak...")
    try:
        token = obter_token(creds)
    except Exception as e:
        log(f"ERRO ao obter token: {e}")
        return 1

    # 2) Buscar o cardápio da semana (almoço + jantar)
    log("Buscando cardápio semanal (almoço)...")
    try:
        almoco_api = buscar_cardapio_semanal(token, 0)
    except Exception as e:
        log(f"ERRO ao buscar almoço: {e}")
        almoco_api = []

    log("Buscando cardápio semanal (jantar)...")
    try:
        jantar_api = buscar_cardapio_semanal(token, 1)
    except Exception as e:
        log(f"ERRO ao buscar jantar: {e}")
        jantar_api = []

    dias = montar_semana(almoco_api, jantar_api)

    if dias:
        titulo = f"Cardápio RU UFG — Semana de {agora.strftime('%d/%m/%Y')}"
        doc_ref.set({
            "titulo": titulo,
            "dias": dias,
            "fonte_pagina_url": RU_WEB_URL + "/tabs/cardapio",
            "atualizado_em": agora.isoformat(timespec="seconds"),
        }, merge=True)
        log(f"Cardápio salvo no Firestore ({len(dias)} dia(s), "
            f"{sum(1 for d in dias if d.get('almoco'))} com almoço, "
            f"{sum(1 for d in dias if d.get('jantar'))} com jantar).")
        dados["dias"] = dias
    else:
        log("! Nenhum dia de cardápio retornado pela API.")

    # 3) Verificar se hoje tem algum prato favorito
    if agora.weekday() == 6:
        log("Domingo: RU fechado, nenhum aviso a enviar hoje.")
        return 0

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

    dias_cardapio = dados.get("dias") or []
    dia_hoje = DIAS_SEMANA[agora.weekday()]
    data_ddmm = agora.strftime("%d/%m")

    dia_obj = None
    for d in dias_cardapio:
        if (normalizar(d.get("dia_semana")) == normalizar(dia_hoje)
                or str(d.get("data", "")).strip() == data_ddmm):
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
            ref = dia_obj.get(refeicao_key)
            if isinstance(ref, dict):
                principal = ref.get("principal") or ""
            achados.append(
                f"• *{label}:* {', '.join(termos_batidos)}"
                + (f" ({principal})" if principal else "")
            )

    if not achados:
        log(f"Hoje ({dia_hoje}) não tem nenhum dos itens marcados.")
        return 0

    unidade = dados.get("unidade") or "RU UFG"
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
