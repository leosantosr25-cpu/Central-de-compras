#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Notificador diário via WhatsApp (CallMeBot) para itens com validade vencendo hoje.

Lê as coleções `products` e `product_expiration` no Firestore (`casas/familia`),
identifica todos os lotes com quantidade > 0 cuja data de validade seja hoje
(fuso America/Sao_Paulo) e dispara uma mensagem resumida listando produto,
marca, quantidade e local de armazenamento.
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

import firebase_admin
from firebase_admin import credentials, firestore

FUSO = ZoneInfo("America/Sao_Paulo")
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def log(*args):
    print(*args, flush=True)


def inferir_local_estoque(prod, lote):
    local = (lote.get("local_estoque") or prod.get("local_estoque") or "").strip()
    if local:
        return local
    cat = (prod.get("category") or "").lower()
    if any(k in cat for k in ["congelad", "carne"]):
        return "Itens no congelador"
    if any(k in cat for k in ["higiene", "limpeza"]):
        return "Itens de higiene"
    return "Itens no armário"


def enviar_whatsapp(mensagem):
    telefone = (os.environ.get("WHATSAPP_PHONE") or "").strip()
    api_key = (os.environ.get("CALLMEBOT_API_KEY") or "").strip()
    if not telefone or not api_key:
        log("! WHATSAPP_PHONE ou CALLMEBOT_API_KEY não configurados; pulando envio.")
        return False

    url = (
        "https://api.callmebot.com/whatsapp.php"
        f"?phone={urllib.parse.quote(telefone)}"
        f"&text={urllib.parse.quote(mensagem)}"
        f"&apikey={urllib.parse.quote(api_key)}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as r:
        log(f"WhatsApp de vencimentos enviado (HTTP {r.status}).")
    return True


def main():
    cred_raw = os.environ.get("FIREBASE_CREDENTIALS")
    if not cred_raw:
        log("ERRO: segredo FIREBASE_CREDENTIALS não configurado.")
        return 1

    cred_dict = json.loads(cred_raw)
    if not firebase_admin._apps:
        firebase_admin.initialize_app(credentials.Certificate(cred_dict))
    db = firestore.client()

    agora = datetime.now(FUSO)
    hoje_iso = agora.date().isoformat()
    hoje_br = agora.strftime("%d/%m/%Y")

    casa_ref = db.collection("casas").document("familia")
    notif_ref = casa_ref.collection("settings").document("notificacoes_vencimento")
    notif_snap = notif_ref.get()
    notif_dados = notif_snap.to_dict() if notif_snap.exists else {}

    if notif_dados.get("ultimo_aviso_data") == hoje_iso:
        log(f"Aviso de vencimentos de hoje ({hoje_iso}) já enviado anteriormente.")
        return 0

    produtos = {}
    for doc in casa_ref.collection("products").stream():
        d = doc.to_dict() or {}
        d["id"] = doc.id
        produtos[doc.id] = d

    vencendo_hoje = []
    for doc in casa_ref.collection("product_expiration").stream():
        lote = doc.to_dict() or {}
        exp = str(lote.get("expiration_date") or "").strip()
        try:
            qtd = float(lote.get("quantity") or 0)
        except (TypeError, ValueError):
            qtd = 0.0
        if qtd <= 0 or exp != hoje_iso:
            continue

        prod = produtos.get(lote.get("product_id"), {})
        nome = (prod.get("name") or "Produto sem nome").strip()
        marca = (prod.get("brand") or "").strip()
        unidade = (prod.get("unit") or "un").strip()
        local = inferir_local_estoque(prod, lote)

        qtd_fmt = f"{int(qtd)}" if qtd.is_integer() else f"{qtd:.2f}".rstrip("0").rstrip(".")
        vencendo_hoje.append({
            "nome": nome,
            "marca": marca,
            "qtd": qtd_fmt,
            "unidade": unidade,
            "local": local,
        })

    if not vencendo_hoje:
        log(f"Nenhum item com validade vencendo hoje ({hoje_iso}).")
        return 0

    linhas = []
    for item in vencendo_hoje:
        titulo = f"{item['nome']} ({item['marca']})" if item["marca"] else item["nome"]
        linhas.append(f"• *{titulo}* — {item['qtd']} {item['unidade']} [{item['local']}]")

    msg = (
        f"⚠️ *Central de Compras — Itens vencendo HOJE ({hoje_br})*\n\n"
        + "\n".join(linhas)
        + "\n\nConfira na aba *Estoque & Validade* do painel."
    )

    if enviar_whatsapp(msg):
        notif_ref.set({
            "ultimo_aviso_data": hoje_iso,
            "itens_notificados": len(vencendo_hoje),
            "atualizado_em": agora.isoformat(timespec="seconds"),
        }, merge=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
