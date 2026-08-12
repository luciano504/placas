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

        completos, remover = [], []
        for p in pend:
            cod = int(p["codigo"])
            n_combo = 0
            m = re.match(r"LEVE\s*(\d+)", str(p.get("obs") or ""))
            if m:
                n_combo = int(m.group(1))
            lojas_alvo = LOJAS if int(p.get("loja") or 0) == 0 else [int(p["loja"])]
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
            remover.append(p["id"])
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
            for _, r in df.iterrows():
                fam = int(r["familia"] or 0)
                cod = int(r["codigo"])
                pai = 1 if (fam == 0 or best.get(fam, (0, 0))[0] == cod) else 0
                w.writerow([cod, str(r["descricao"] or "").strip(),
                            f'{float(r["preconormal"] or 0):.2f}',
                            f'{float(r["precooferta"] or 0):.2f}',
                            r["inicio"], r["fim"], str(r["secao"] or "").strip(),
                            fam, pai, "", ""])
        total += len(df)
        print(f"loja{loja}: {len(df)} promoções vigentes.")
        time.sleep(2)

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
