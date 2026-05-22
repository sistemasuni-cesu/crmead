"""
CRM Unicesumar — Servidor
Duplo clique para iniciar, ou: python servidor.py

Instalar dependências (uma vez só):
    pip install flask

Acesso: http://localhost:5000
"""

import sqlite3
import json
import webbrowser
import threading
import os
from pathlib import Path
from datetime import datetime
from flask import Flask, jsonify, request, send_from_directory, Response

# ── Configuração ──────────────────────────────────────────────
ONEDRIVE = Path.home() / "OneDrive"
DB_PATH  = ONEDRIVE / "CRM_Unicesumar" / "crm_unicesumar.db"
if not DB_PATH.exists():
    DB_PATH = Path(__file__).parent / "crm_unicesumar.db"

STATIC_DIR = Path(__file__).parent
app = Flask(__name__, static_folder=str(STATIC_DIR))

MEU_NOME = "LUIZ EDUARDO FERREIRA PALMA"

# ── Helpers ───────────────────────────────────────────────────

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def rows_to_list(rows):
    return [dict(r) for r in rows]


# ── Rotas: painel ─────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "painel.html")


# ── Rotas: dados ──────────────────────────────────────────────

@app.route("/api/semestres")
def semestres():
    conn = get_conn()
    rows = conn.execute("SELECT DISTINCT semestre FROM inscricoes ORDER BY semestre DESC").fetchall()
    conn.close()
    return jsonify([r["semestre"] for r in rows])


@app.route("/api/polos")
def polos():
    sem = request.args.get("semestre", "")
    conn = get_conn()
    q = "SELECT DISTINCT polo FROM inscricoes WHERE polo IS NOT NULL"
    params = []
    if sem:
        q += " AND semestre=?"
        params.append(sem)
    q += " ORDER BY polo"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return jsonify([r["polo"] for r in rows if r["polo"]])


@app.route("/api/dashboard")
def dashboard():
    sem  = request.args.get("semestre", "")
    polo = request.args.get("polo", "")
    meus = request.args.get("meus", "0")

    conn = get_conn()
    where, params = _filtros_base(sem, polo, meus)

    total     = conn.execute(f"SELECT COUNT(*) FROM inscricoes {where}", params).fetchone()[0]
    sem_conta = conn.execute(f"SELECT COUNT(*) FROM inscricoes {where} AND (dias_sem_contato IS NULL OR CAST(dias_sem_contato AS INT) > 7)", params).fetchone()[0]
    boleto_av = conn.execute(f"SELECT COUNT(*) FROM inscricoes {where} AND mensalidade LIKE '%VENCER%'", params).fetchone()[0]
    desistiu  = conn.execute(f"SELECT COUNT(*) FROM inscricoes {where} AND (desistiu='SIM' OR cancelou='SIM')", params).fetchone()[0]

    por_status = rows_to_list(conn.execute(
        f"SELECT status, COUNT(*) as total FROM inscricoes {where} GROUP BY status ORDER BY total DESC LIMIT 10", params
    ).fetchall())

    por_polo = rows_to_list(conn.execute(
        f"SELECT polo, COUNT(*) as total FROM inscricoes {where} GROUP BY polo ORDER BY total DESC LIMIT 10", params
    ).fetchall())

    conn.close()
    return jsonify({
        "total": total,
        "sem_contato": sem_conta,
        "boleto_avencer": boleto_av,
        "desistiu": desistiu,
        "por_status": por_status,
        "por_polo": por_polo,
    })


@app.route("/api/inscricoes")
def inscricoes():
    sem      = request.args.get("semestre", "")
    polo     = request.args.get("polo", "")
    meus     = request.args.get("meus", "0")
    busca    = request.args.get("busca", "")
    status   = request.args.get("status", "")
    ordem    = request.args.get("ordem", "nome")
    direcao  = request.args.get("direcao", "ASC")
    pagina   = int(request.args.get("pagina", 1))
    por_pag  = int(request.args.get("por_pagina", 50))
    
    # Novos filtros de Painel e Datas
    painel      = request.args.get("painel", "todos")
    insc_de     = request.args.get("insc_de", "")
    insc_ate    = request.args.get("insc_ate", "")
    contato_de  = request.args.get("contato_de", "")
    contato_ate = request.args.get("contato_ate", "")

    conn = get_conn()
    where, params = _filtros_base(sem, polo, meus)

    if busca:
        where += " AND (nome LIKE ? OR candidato LIKE ? OR cpf LIKE ? OR email LIKE ?)"
        b = f"%{busca}%"
        params += [b, b, b, b]

    if status:
        where += " AND status=?"
        params.append(status)

    # Filtros de Intervalo de Datas (Tratando formatos comuns YYYY-MM-DD)
    if insc_de:
        where += " AND dt_inscricao >= ?"
        params.append(insc_de)
    if insc_ate:
        where += " AND dt_inscricao <= ?"
        params.append(insc_ate)
        
    if contato_de:
        where += " AND ultimo_contato >= ?"
        params.append(contato_de)
    if contato_ate:
        where += " AND ultimo_contato <= ?"
        params.append(contato_ate)

    # Lógica de Separação de Painéis Temáticos
    if painel == "pre":
        where += " AND (status LIKE '%INSCRIT%' OR status LIKE '%PRÉ%')"
    elif painel == "vestibular":
        where += " AND (status LIKE '%VESTIBULAR%' OR status LIKE '%AGUARD%')"
    elif painel == "boletos":
        where += " AND (status LIKE '%BOLETO%' OR mensalidade LIKE '%VENCER%')"
    elif painel == "finalizados":
        where += " AND (status LIKE '%MATRICUL%' OR status LIKE '%RA%' OR desistiu='SIM' OR cancelou='SIM')"

    colunas_validas = ["nome","candidato","cpf","curso","polo","status","mensalidade",
                       "ultimo_contato","dias_sem_contato","inscrito_por","boleto_gerado_por",
                       "resp_ultimo_atendimento","semestre","telefone","celular","email",
                       "total_contatos","atendimento","motivo","observacoes_original","anotacoes","id","dt_inscricao"]
    if ordem not in colunas_validas:
        ordem = "nome"
    if direcao not in ("ASC","DESC"):
        direcao = "ASC"

    total_q = conn.execute(f"SELECT COUNT(*) FROM inscricoes {where}", params).fetchone()[0]
    offset  = (pagina - 1) * por_pag
    rows    = conn.execute(
        f"SELECT * FROM inscricoes {where} ORDER BY {ordem} {direcao} LIMIT ? OFFSET ?",
        params + [por_pag, offset]
    ).fetchall()
    conn.close()

    return jsonify({"total": total_q, "pagina": pagina, "registros": rows_to_list(rows)})


@app.route("/api/inscricoes/<int:iid>", methods=["GET"])
def get_inscricao(iid):
    conn = get_conn()
    row = conn.execute("SELECT * FROM inscricoes WHERE id=?", (iid,)).fetchone()
    anotacoes = conn.execute(
        "SELECT * FROM anotacoes WHERE candidato_id=? ORDER BY criado_em DESC", (str(iid),)
    ).fetchall()
    conn.close()
    if not row:
        return jsonify({"erro": "não encontrado"}), 404
    return jsonify({"inscricao": dict(row), "anotacoes": rows_to_list(anotacoes)})


@app.route("/api/inscricoes/<int:iid>/anotacao", methods=["POST"])
def add_anotacao(iid):
    data  = request.json
    texto = data.get("texto", "").strip()
    autor = data.get("autor", MEU_NOME)
    if not texto:
        return jsonify({"erro": "texto vazio"}), 400
    conn = get_conn()
    conn.execute(
        "INSERT INTO anotacoes (candidato_id, texto, autor, criado_em) VALUES (?,?,?,?)",
        (str(iid), texto, autor, datetime.now().isoformat())
    )
    conn.execute(
        "UPDATE inscricoes SET anotacoes=? WHERE id=?",
        (f"[{datetime.now().strftime('%d/%m/%y')}] {texto}", iid)
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/status_lista")
def status_lista():
    sem = request.args.get("semestre", "")
    conn = get_conn()
    q = "SELECT DISTINCT status FROM inscricoes WHERE status IS NOT NULL"
    params = []
    if sem:
        q += " AND semestre=?"
        params.append(sem)
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return jsonify([r["status"] for r in rows if r["status"]])


@app.route("/api/exportar")
def exportar():
    sem   = request.args.get("semestre", "")
    polo  = request.args.get("polo", "")
    meus  = request.args.get("meus", "0")
    busca = request.args.get("busca", "")
    status = request.args.get("status", "")
    painel = request.args.get("painel", "todos")

    conn = get_conn()
    where, params = _filtros_base(sem, polo, meus)
    if busca:
        where += " AND (nome LIKE ? OR candidato LIKE ? OR cpf LIKE ?)"
        b = f"%{busca}%"
        params += [b, b, b]
    if status:
        where += " AND status=?"
        params.append(status)
        
    if painel == "pre":
        where += " AND (status LIKE '%INSCRIT%' OR status LIKE '%PRÉ%')"
    elif painel == "vestibular":
        where += " AND (status LIKE '%VESTIBULAR%' OR status LIKE '%AGUARD%')"
    elif painel == "boletos":
        where += " AND (status LIKE '%BOLETO%' OR mensalidade LIKE '%VENCER%')"
    elif painel == "finalizados":
        where += " AND (status LIKE '%MATRICUL%' OR status LIKE '%RA%' OR desistiu='SIM' OR cancelou='SIM')"

    rows = conn.execute(f"SELECT * FROM inscricoes {where} ORDER BY nome", params).fetchall()
    conn.close()

    if not rows:
        return jsonify({"erro": "sem dados"}), 404

    cols = rows[0].keys()
    lines = [";".join(cols)]
    for r in rows:
        lines.append(";".join(str(r[c] or "") for c in cols))
    csv_content = "\n".join(lines)

    return Response(
        csv_content,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=crm_export_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"}
    )


def _filtros_base(sem, polo, meus):
    where  = "WHERE 1=1"
    params = []
    if sem:
        where += " AND semestre=?"
        params.append(sem)
    if polo:
        where += " AND polo=?"
        params.append(polo)
    if meus == "1":
        like = f"%{MEU_NOME}%"
        where += " AND (inscrito_por LIKE ? OR boleto_gerado_por LIKE ? OR resp_ultimo_atendimento LIKE ?)"
        params += [like, like, like]
    return where, params


if __name__ == "__main__":
    if not DB_PATH.exists():
        print(f"⚠️  Banco não encontrado em: {DB_PATH}")
        print("   Execute importar.py primeiro para criar o banco.")
        input("Pressione Enter para fechar...")
    else:
        print("=" * 45)
        print("  CRM Unicesumar — Servidor iniciado")
        print("  Acesse: http://localhost:5000")
        print("  Ctrl+C para parar")
        print("=" * 45)
        # Se der erro de novo, a linha abaixo comentada com '#' evita o problema:
        # threading.Thread(target=abrir_browser, daemon=True).start()
        app.run(host="127.0.0.1", port=5000, debug=False)