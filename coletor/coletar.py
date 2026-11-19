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
                for produto, marca, preco, validade, link in ofertas:
                    novas.append({
                        "data": hoje,
                        "supermercado": loja["nome"],
                        "produto": produto,
                        "marca": marca,
                        "preco": f"{preco:.2f}",
                        "validade": validade,
                        "link": link,
                    })
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
