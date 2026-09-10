# -*- coding: utf-8 -*-
"""Gera os dados de promoções vigentes por loja para o app de Placas de Oferta.

Roda no GitHub Actions (agendado) e grava data/lojaN.csv + data/meta.json.
O app (index.html) busca esses arquivos no próprio site.
"""
import csv
import io
import re
import json
import time
from datetime import datetime, timezone, timedelta

import pandas as pd
import requests

VR_URL = "http://rendemaisdns.zapto.org:8086/teste/"
LOJAS = [1, 2, 3, 4, 5, 8, 9]
RECIFE = timezone(timedelta(hours=-3))

SB_URL = "https://estciwkeihmokvlnvaum.supabase.co"
SB_KEY = "sb_publishable_l14fjxQmWUeu5OXlZJ35GA_-X3ESkgZ"
SBH = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}",
       "Content-Type": "application/json"}


def sb_get(path):
    r = requests.get(f"{SB_URL}/rest/v1/{path}", headers=SBH, timeout=60)
    r.raise_for_status()
    return r.json()


def query_vr(sql, max_retries=4):
    last = None
    for att in range(1, max_retries + 1):
        try:
            r = requests.post(VR_URL, data={"sql_query": sql, "export_type": "csv"},
                              timeout=900)
            r.raise_for_status()
            ini = r.text.lstrip()[:100].lower()
            if ini.startswith("<!doctype") or ini.startswith("<html") or "fatal error" in ini:
                # Bug do endpoint: consulta com 0 linhas derruba o streamCsv() do PHP.
                if "streamCsv" in r.text and "firstRow" in r.text:
                    return pd.DataFrame()
                raise RuntimeError(f"Erro VR: {r.text[:200]}")
            df = pd.read_csv(io.StringIO(r.text))
            df.columns = [str(c).lstrip("﻿").strip() for c in df.columns]
            return df
        except Exception as e:  # noqa: BLE001
            last = e
            if att < max_retries:
                time.sleep(min(60, 10 * att))
    raise RuntimeError(f"Query VR falhou: {last}")


def enriquecer_campanhas():
    """Completa (descrição + preço 'De' do VR) as campanhas pontuais criadas na
    aba do aplicativo, e regrava data/campanhas.csv a partir do Supabase."""
    try:
        pend = sb_get("campanhas_placas?select=*&pendente=eq.true")
    except Exception as e:  # noqa: BLE001
        print(f"campanhas: tabela indisponível ({e}); mantendo data/campanhas.csv atual.")
        return

    if pend:
        ids = sorted({int(p["codigo"]) for p in pend})
        lista = ",".join(str(i) for i in ids)
        # Lojão de Cosméticos (loja 4) só entra na expansão "todas as lojas"
        # se o produto de fato vende lá (últimos 60 dias)
        vende_l4 = set()
        try:
            v4 = query_vr(f"""
                SELECT DISTINCT vi.id_produto AS codigo
                FROM pdv.vendaitem vi
                JOIN pdv.venda v ON v.id = vi.id_venda
                WHERE v.id_loja = 4 AND vi.data >= CURRENT_DATE - 60
                  AND vi.id_produto IN ({lista})""")
            vende_l4 = {int(r["codigo"]) for _, r in v4.iterrows()}
        except Exception as e:  # noqa: BLE001
            print(f"campanhas: checagem de vendas L04 falhou ({e}); L04 fica de fora da expansão.")
        info = query_vr(f"""
            SELECT p.id AS codigo, p.descricaocompleta AS descricao,
                   COALESCE(m.descricao,'OUTROS') AS secao
            FROM produto p
            LEFT JOIN mercadologico m ON m.nivel = 1 AND m.mercadologico1 = p.mercadologico1
            WHERE p.id IN ({lista})""")
        precos = query_vr(f"""
            SELECT id_produto AS codigo, id_loja, precovenda, precovendaanterior
            FROM produtocomplemento
            WHERE id_produto IN ({lista}) AND id_loja IN (1,2,3,4,5,8,9)""")
        desc, sec = {}, {}
        for _, r in info.iterrows():
            desc[int(r["codigo"])] = " ".join(str(r["descricao"] or "").split())
            sec[int(r["codigo"])] = str(r["secao"] or "OUTROS").strip()
        pv, pva = {}, {}
        for _, r in precos.iterrows():
            k = (int(r["codigo"]), int(r["id_loja"]))
            pv[k] = float(r["precovenda"] or 0)
            pva[k] = float(r["precovendaanterior"] or 0)

        if not pv:
            # VR não devolveu preço nenhum (fora do ar / erro) — não mexe nos
            # pendentes: ficam guardados para a próxima tentativa.
            print(f"campanhas: VR sem resposta de preços para {len(pend)} pendentes; "
                  "mantidos para a próxima atualização.")
            pend = []

        completos, remover, sem_preco = [], [], []
        for p in pend:
            cod = int(p["codigo"])
            gerou_antes = len(completos)
            n_combo = 0
            m = re.match(r"LEVE\s*(\d+)", str(p.get("obs") or ""))
            if m:
                n_combo = int(m.group(1))
            lojas_alvo = ([lj for lj in LOJAS if lj != 4 or cod in vende_l4]
                          if int(p.get("loja") or 0) == 0 else [int(p["loja"])])
            for lj in lojas_alvo:
                de = pv.get((cod, lj), 0)
                if not de or de >= 9000:   # sem preço nessa loja (ex.: açougue no Lojão)
                    continue
                por = p.get("por")
                if n_combo >= 2:
                    # combo "N por R$ 10": De e Por = preço avulso; valor total fica no obs
                    # se o avulso já estiver promocionado (N×avulso <= total), usa o maior da rede
                    tot = 10.0
                    m2 = re.search(r"(\d+[.,]\d{2})", str(p["obs"]))
                    if m2:
                        tot = float(m2.group(1).replace(",", "."))
                    if de * n_combo <= tot:
                        de = max([pv.get((cod, x), 0) for x in LOJAS] + [de])
                    por = de
                else:
                    por = float(por or 0)
                    if not por:
                        continue
                    if de <= por:  # precovenda já é o promocional → usa o anterior
                        ant = pva.get((cod, lj), 0)
                        de = ant if ant > por else max([pv.get((cod, x), 0) for x in LOJAS] + [de])
                    if de <= por:
                        de = por
                completos.append({
                    "camp": p["camp"], "loja": lj, "codigo": cod,
                    "descricao": desc.get(cod, f"PRODUTO {cod}"),
                    "secao": sec.get(cod, "OUTROS"),
                    "de": round(de, 2), "por": round(float(por), 2),
                    "obs": p.get("obs") or "", "inicio": p["inicio"], "fim": p["fim"],
                    "pendente": False,
                })
            if len(completos) > gerou_antes:
                remover.append(p["id"])   # só remove o pendente que virou placa
            else:
                sem_preco.append(cod)     # sem preço no VR: continua pendente
        if sem_preco:
            print(f"campanhas: sem preço/cadastro no VR (seguem pendentes): "
                  f"{sorted(set(sem_preco))}")
        if completos:
            r = requests.post(f"{SB_URL}/rest/v1/campanhas_placas",
                              headers={**SBH, "Prefer": "return=minimal"},
                              json=completos, timeout=60)
            r.raise_for_status()
        if remover:
            lista_ids = ",".join(str(i) for i in remover)
            requests.delete(f"{SB_URL}/rest/v1/campanhas_placas?id=in.({lista_ids})",
                            headers=SBH, timeout=60).raise_for_status()
        print(f"campanhas: {len(pend)} pendentes → {len(completos)} linhas completas.")

    # regrava data/campanhas.csv (fallback do app) com tudo que está vigente/futuro
    hoje = datetime.now(RECIFE).strftime("%Y-%m-%d")
    rows = sb_get(f"campanhas_placas?select=*&pendente=eq.false&fim=gte.{hoje}"
                  "&order=camp,codigo,loja")
    with open("data/campanhas.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
        w.writerow(["loja", "codigo", "descricao", "preconormal", "precooferta",
                    "inicio", "fim", "secao", "familia", "pai", "camp", "obs"])
        def br(iso):
            p = str(iso)[:10].split("-")
            return f"{p[2]}/{p[1]}"
        for c in rows:
            w.writerow([c["loja"], c["codigo"], c.get("descricao") or "",
                        f'{float(c.get("de") or 0):.2f}', f'{float(c.get("por") or 0):.2f}',
                        br(c["inicio"]), br(c["fim"]), c.get("secao") or "OUTROS",
                        0, 1, c.get("camp") or "", c.get("obs") or ""])
    print(f"campanhas: data/campanhas.csv regravado com {len(rows)} linhas.")


CAMPS_ENCARTE = ("ofsem", "offds")   # Oferta da Semana / Oferta do Fim de Semana


def campanhas_do_encarte():
    """Traz para as placas os itens já LIBERADOS no app do encarte.

    O app do encarte (encarte.html + encarte.php, na pasta do App SQL) grava a
    lista da semana em rm_encarte_item e o OK final em rm_encarte_aprovacao,
    ambos no próprio Postgres do VR. Aqui a gente lê o que está liberado e
    coloca em campanhas_placas, para a placa ficar pronta ANTES de a central
    lançar a oferta no VR.

    Item que a central já lançou no VR NÃO é mais descartado (era assim até
    10/09/2026 e deixava a campanha "Oferta do Fim de Semana" quase vazia no
    app: os itens estavam lá, mas soltos no meio das promoções do VR, sem
    etiqueta de campanha). Agora ele continua na campanha e quem sai é a linha
    correspondente do data/lojaN.csv — por isso esta função devolve o conjunto
    de pares (código, loja) publicados, que o main() usa para não repetir.

    Quando o item já está no VR, o "de/por" vem do VR, não do encarte: o preço
    da placa tem de bater com o preço do caixa.
    """
    try:
        itens = query_vr("""
            SELECT i.semana, i.momento, i.id_loja, i.id_produto AS codigo,
                   i.descricao, i.secao, i.preco_normal, i.preco_oferta,
                   i.tipo, i.qtd_min, i.preco_esc,
                   to_char(CASE WHEN i.momento = 'chumbo' THEN i.semana + 3 ELSE i.semana END,
                           'YYYY-MM-DD') AS inicio,
                   to_char(CASE WHEN i.momento = 'chumbo' THEN i.semana + 5 ELSE i.semana + 6 END,
                           'YYYY-MM-DD') AS fim
            FROM rm_encarte_item i
            JOIN rm_encarte_aprovacao a
              ON a.semana = i.semana AND a.momento = i.momento
            WHERE i.ativo
              AND (CASE WHEN i.momento = 'chumbo' THEN i.semana + 5 ELSE i.semana + 6 END)
                  >= CURRENT_DATE""")
    except Exception as e:  # noqa: BLE001
        print(f"encarte: não consegui ler a lista liberada ({e}); placas seguem sem ela.")
        return set()

    if itens is None or itens.empty:
        print("encarte: nenhuma lista liberada e vigente.")
    # preço que o VR já tem para o item (é ele que vale na placa)
    preco_vr = {}
    try:
        of = query_vr("""
            SELECT DISTINCT ON (o.id_produto, o.id_loja)
                   o.id_produto AS codigo, o.id_loja, o.preconormal, o.precooferta
            FROM oferta o
            WHERE o.id_situacaooferta = 1
              AND o.datainicio <= CURRENT_DATE + 7 AND o.datatermino >= CURRENT_DATE
            ORDER BY o.id_produto, o.id_loja, o.datainicio DESC, o.datatermino ASC""")
        if of is not None and not of.empty:
            for _, r in of.iterrows():
                preco_vr[(int(r["codigo"]), int(r["id_loja"]))] = (
                    float(r["preconormal"] or 0), float(r["precooferta"] or 0))
    except Exception as e:  # noqa: BLE001
        print(f"encarte: não consegui ler as ofertas já lançadas ({e}); uso o preço do encarte.")

    # preço anterior de gôndola — última cartada para o "de" quando o VR lançou
    # a oferta com preconormal igual ao precooferta (mesma regra que o
    # enriquecer_campanhas() já usa nas campanhas pontuais)
    anterior = {}
    try:
        cods = sorted({int(r["codigo"]) for _, r in itens.iterrows()}) if (
            itens is not None and not itens.empty) else []
        if cods:
            pc = query_vr(f"""
                SELECT id_produto AS codigo, id_loja, precovendaanterior
                FROM produtocomplemento
                WHERE id_produto IN ({','.join(str(c) for c in cods)})
                  AND id_loja IN (1,2,3,5,8,9)""")
            if pc is not None and not pc.empty:
                for _, r in pc.iterrows():
                    anterior[(int(r["codigo"]), int(r["id_loja"]))] = float(
                        r["precovendaanterior"] or 0)
    except Exception as e:  # noqa: BLE001
        print(f"encarte: não consegui ler o preço anterior ({e}); sigo sem ele.")

    lojas_super = [1, 2, 3, 5, 8, 9]   # o encarte é das 6 de supermercado; L04 fica fora
    linhas, publicados, do_vr, sem_desconto = [], set(), 0, []
    for _, r in (itens.iterrows() if itens is not None and not itens.empty else []):
        cod = int(r["codigo"])
        camp = "offds" if str(r["momento"]).strip() == "chumbo" else "ofsem"
        alvo = lojas_super if int(r["id_loja"] or 0) == 0 else [int(r["id_loja"])]
        de = float(r["preco_normal"] or 0)
        por = float(r["preco_oferta"] or 0)
        if por <= 0:
            continue
        if de <= por:
            de = por

        # condição da oferta vira o texto da placa (o app já interpreta "LEVE N ...")
        tipo = str(r["tipo"] or "simples").strip()
        obs = ""
        try:
            qmin = float(r["qtd_min"] or 0)
            pesc = float(r["preco_esc"] or 0)
        except Exception:  # noqa: BLE001
            qmin = pesc = 0
        if tipo != "simples" and qmin > 0 and pesc > 0:
            nq = str(int(qmin)) if float(qmin).is_integer() else f"{qmin:.3f}".rstrip("0").rstrip(".")
            val = f"{pesc:.2f}".replace(".", ",")
            if tipo == "leve":
                # o "CADA" é o que faz a placa tratar o valor como unitário
                obs = f"LEVE {nq} POR R$ {val} CADA"
            elif tipo == "combo":
                obs = f"LEVE {nq} POR R$ {val}"          # sem "CADA" = valor total
            elif tipo == "peso":
                obs = f"ACIMA DE {nq} KG, O KG SAI POR R$ {val}"

        for lj in alvo:
            # Se a central já lançou no VR, o preço da placa é o do VR (é o que
            # o cliente vai pagar no caixa). O texto da condição continua vindo
            # do encarte: o VR não guarda "acima de 1 kg, o kg sai por...".
            de_lj, por_lj = de, por
            if (cod, lj) in preco_vr:
                v_de, v_por = preco_vr[(cod, lj)]
                if v_por > 0:
                    por_lj = v_por
                    # o "de" tem de ser maior que o "por", senão a placa sai
                    # "de 29,99 por 29,99". Ordem: preço normal do VR, o preço
                    # normal que o encarte guardou, o preço anterior de gôndola.
                    ant = anterior.get((cod, lj), 0)
                    if v_de > v_por:
                        de_lj = v_de
                    elif de > v_por:
                        de_lj = de
                    elif ant > v_por:
                        de_lj = ant
                    else:
                        de_lj = v_por
                do_vr += 1
            if de_lj <= por_lj:
                sem_desconto.append((cod, lj))
            linhas.append({
                "camp": camp, "loja": lj, "codigo": cod,
                "descricao": " ".join(str(r["descricao"] or "").split()),
                "secao": str(r["secao"] or "OUTROS").strip(),
                "de": round(de_lj, 2), "por": round(por_lj, 2), "obs": obs,
                "inicio": r["inicio"], "fim": r["fim"], "pendente": False,
            })
            publicados.add((cod, lj))

    # troca o bloco do encarte inteiro (apaga o anterior e regrava)
    try:
        lista = ",".join(CAMPS_ENCARTE)
        requests.delete(f"{SB_URL}/rest/v1/campanhas_placas?camp=in.({lista})",
                        headers=SBH, timeout=60).raise_for_status()
        if linhas:
            r = requests.post(f"{SB_URL}/rest/v1/campanhas_placas",
                              headers={**SBH, "Prefer": "return=minimal"},
                              json=linhas, timeout=60)
            r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        print(f"encarte: falhou ao gravar em campanhas_placas ({e}).")
        return set()
    print(f"encarte: {len(linhas)} linhas liberadas para as placas "
          f"({do_vr} com preço vindo do VR, já lançado pela central).")
    if sem_desconto:
        cods_sd = sorted({c for c, _ in sem_desconto})
        print(f"encarte: ATENÇÃO — sem desconto (de = por), a placa sai errada: {cods_sd}")
    return publicados


def main():
    # vendas 30d (rede) dos produtos em promoção vigente — para eleger o produto pai
    vendas = query_vr("""
        SELECT v.id_produto AS codigo, SUM(v.quantidade) AS qtd30
        FROM pdv.vendaitem v
        WHERE v.data >= CURRENT_DATE - 30
          AND v.id_produto IN (
              SELECT DISTINCT id_produto FROM oferta
              WHERE id_situacaooferta = 1
                AND datainicio <= CURRENT_DATE + 7 AND datatermino >= CURRENT_DATE)
        GROUP BY v.id_produto""")
    qtd = {}
    for _, r in vendas.iterrows():
        try:
            qtd[int(r["codigo"])] = float(r["qtd30"])
        except Exception:  # noqa: BLE001
            pass

    # O encarte grava PRIMEIRO e devolve os pares (código, loja) que saem com
    # etiqueta de campanha. Esses ficam de fora do data/lojaN.csv logo abaixo,
    # senão o mesmo item apareceria duas vezes na lista do app.
    try:
        do_encarte = campanhas_do_encarte()
    except Exception as e:  # noqa: BLE001
        print(f"encarte: falhou ({e}); dados principais seguem normais.")
        do_encarte = set()

    total = 0
    for loja in LOJAS:
        df = query_vr(f"""
            SELECT DISTINCT ON (o.id_produto)
                   o.id_produto AS codigo, p.descricaocompleta AS descricao,
                   o.preconormal, o.precooferta,
                   to_char(o.datainicio,'DD/MM') AS inicio,
                   to_char(o.datatermino,'DD/MM') AS fim,
                   COALESCE(m.descricao,'OUTROS') AS secao,
                   COALESCE(p.id_familiaproduto,0) AS familia
            FROM oferta o
            JOIN produto p ON p.id = o.id_produto
            LEFT JOIN mercadologico m ON m.nivel = 1 AND m.mercadologico1 = p.mercadologico1
            WHERE o.id_loja = {loja} AND o.id_situacaooferta = 1
              AND o.datainicio <= CURRENT_DATE + 7 AND o.datatermino >= CURRENT_DATE
            ORDER BY o.id_produto, o.datainicio DESC, o.datatermino ASC""")

        # produto pai = mais vendido (30d) de cada família
        best = {}
        for _, r in df.iterrows():
            fam = int(r["familia"] or 0)
            if fam == 0:
                continue
            q = qtd.get(int(r["codigo"]), 0)
            if fam not in best or q > best[fam][1]:
                best[fam] = (int(r["codigo"]), q)

        with open(f"data/loja{loja}.csv", "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
            w.writerow(["codigo", "descricao", "preconormal", "precooferta",
                        "inicio", "fim", "secao", "familia", "pai", "camp", "obs"])
            escritas = 0
            for _, r in df.iterrows():
                fam = int(r["familia"] or 0)
                cod = int(r["codigo"])
                if (cod, loja) in do_encarte:
                    continue          # já vai sair etiquetado como campanha do encarte
                escritas += 1
                pai = 1 if (fam == 0 or best.get(fam, (0, 0))[0] == cod) else 0
                w.writerow([cod, str(r["descricao"] or "").strip(),
                            f'{float(r["preconormal"] or 0):.2f}',
                            f'{float(r["precooferta"] or 0):.2f}',
                            r["inicio"], r["fim"], str(r["secao"] or "").strip(),
                            fam, pai, "", ""])
        total += escritas
        movidas = len(df) - escritas
        print(f"loja{loja}: {escritas} promoções vigentes"
              + (f" ({movidas} saíram como campanha do encarte)." if movidas else "."))
        time.sleep(2)

    # enriquecer_campanhas() regrava data/campanhas.csv a partir do Supabase —
    # como o encarte já gravou lá em cima, o arquivo de reserva sai completo.
    try:
        enriquecer_campanhas()
    except Exception as e:  # noqa: BLE001
        print(f"campanhas: falhou ({e}); dados principais seguem normais.")

    agora = datetime.now(RECIFE)
    with open("data/meta.json", "w", encoding="utf-8") as f:
        json.dump({"gerado_em": agora.strftime("%d/%m %H:%M"),
                   "gerado_em_iso": agora.isoformat()}, f, ensure_ascii=False)
    print(f"Total: {total} linhas. Gerado em {agora.strftime('%d/%m %H:%M')} (Recife).")


if __name__ == "__main__":
    main()
