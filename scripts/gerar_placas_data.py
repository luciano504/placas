# -*- coding: utf-8 -*-
"""Gera os dados de promoções vigentes por loja para o app de Placas de Oferta.

Roda no GitHub Actions (agendado) e grava data/lojaN.csv + data/meta.json.
O app (index.html) busca esses arquivos no próprio site.
"""
import csv
import io
import json
import time
from datetime import datetime, timezone, timedelta

import pandas as pd
import requests

VR_URL = "http://rendemaisdns.zapto.org:8086/teste/"
LOJAS = [1, 2, 3, 4, 5, 8, 9]
RECIFE = timezone(timedelta(hours=-3))


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
            ORDER BY o.id_produto, o.datatermino ASC""")

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

    agora = datetime.now(RECIFE)
    with open("data/meta.json", "w", encoding="utf-8") as f:
        json.dump({"gerado_em": agora.strftime("%d/%m %H:%M"),
                   "gerado_em_iso": agora.isoformat()}, f, ensure_ascii=False)
    print(f"Total: {total} linhas. Gerado em {agora.strftime('%d/%m %H:%M')} (Recife).")


if __name__ == "__main__":
    main()
