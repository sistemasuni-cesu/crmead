"""
CRM Unicesumar — Servidor
Deploy: Railway (ou qualquer host com Python)

Fluxo de atualização diária:
  1. Você roda seu script → gera crm_unicesumar.db (com anotações da planilha)
  2. O .db é salvo automaticamente no Google Drive
  3. Configure a variável GDRIVE_URL no Railway (link de compartilhamento)
  4. O servidor puxa o .db novo a cada 3 minutos automaticamente

Como as anotações funcionam:
  - Anotações feitas ONLINE são salvas no PostgreSQL do Railway (persistem para sempre)
  - Anotações vindas da PLANILHA chegam dentro do .db via campo `anotacoes` da tabela inscricoes
    e também pela tabela `anotacoes` — ambas são mescladas na exibição
  - A mesclagem é feita por candidato_id + texto (sem duplicatas)
  - Nunca se perde nada: novo .db não apaga anotações online

Variáveis de ambiente no Railway:
  DATABASE_URL   → gerada automaticamente ao adicionar PostgreSQL no Railway
  GDRIVE_URL     → link de compartilhamento do .db no Google Drive
  MEU_NOME       → seu nome completo (padrão já configurado abaixo)
  DB_REFRESH_INTERVAL → intervalo em segundos para baixar o .db (padrão: 180)

Instalar dependências locais:
    pip install flask requests psycopg2-binary

Deploy Railway:
    Procfile        → web: gunicorn servidor:app --bind 0.0.0.0:$PORT
    requirements.txt → flask gunicorn requests psycopg2-binary
"""

import sqlite3
import os
import tempfile
import threading
import time
import requests
import psycopg2
import psycopg2.extras
from pathlib import Path
from datetime import datetime
from flask import Flask, jsonify, request, send_from_directory, Response

# ── Configuração ──────────────────────────────────────────────

GDRIVE_URL        = os.environ.get("GDRIVE_URL", "COLE_O_LINK_AQUI")
REFRESH_INTERVAL  = int(os.environ.get("DB_REFRESH_INTERVAL", 180))
DATABASE_URL      = os.environ.get("DATABASE_URL", "")          # PostgreSQL no Railway
MEU_NOME          = os.environ.get("MEU_NOME", "LUIZ EDUARDO FERREIRA PALMA")

STATIC_DIR = Path(__file__).parent
app = Flask(__name__, static_folder=str(STATIC_DIR))

# ── Cache do SQLite (Google Drive) ────────────────────────────

_db_path: Path  = None
_db_lock        = threading.Lock()
_last_fetch: float = 0


def _gdrive_direct_url(share_url: str) -> str:
    """
    Converte link de compartilhamento do Google Drive em link de download direto.
    Suporta formatos:
      https://drive.google.com/file/d/FILE_ID/view?usp=sharing
      https://drive.google.com/open?id=FILE_ID
    """
    import re
    # Formato /file/d/ID/view
    match = re.search(r"/d/([a-zA-Z0-9_-]+)", share_url)
    if match:
        file_id = match.group(1)
        return f"https://drive.google.com/uc?export=download&id={file_id}&confirm=t"
    # Formato ?id=ID
    match = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", share_url)
    if match:
        file_id = match.group(1)
        return f"https://drive.google.com/uc?export=download&id={file_id}&confirm=t"
    # Fallback: retorna como está
    return share_url


def _baixar_banco() -> Path:
    url  = _gdrive_direct_url(GDRIVE_URL)
    resp = requests.get(url, timeout=30, allow_redirects=True)
    resp.raise_for_status()

    # Verifica se o conteúdo é realmente um banco SQLite (começa com "SQLite format 3")
    content = resp.content
    if not content.startswith(b"SQLite format 3"):
        raise ValueError(
            f"Conteúdo baixado não é um banco SQLite válido. "
            f"Verifique se o arquivo no Google Drive está compartilhado como 'Qualquer pessoa com o link'. "
            f"Primeiros bytes: {content[:50]}"
        )

    tmp = tempfile.NamedTemporaryFile(suffix=".db", prefix="crm_", delete=False)
    tmp.write(content)
    tmp.flush()
    tmp.close()
    return Path(tmp.name)


def get_db_path() -> Path:
    global _db_path, _last_fetch
    if not GDRIVE_URL or GDRIVE_URL == "COLE_O_LINK_AQUI":
        fallback = Path(__file__).parent / "crm_unicesumar.db"
        if fallback.exists():
            return fallback
        raise FileNotFoundError("Banco não encontrado. Configure GDRIVE_URL.")
    agora = time.time()
    with _db_lock:
        if _db_path is None or (agora - _last_fetch) > REFRESH_INTERVAL:
            try:
                novo   = _baixar_banco()
                antigo = _db_path
                _db_path   = novo
                _last_fetch = agora
                if antigo and antigo.exists():
                    try: antigo.unlink()
                    except Exception: pass
                print(f"[DB] Banco atualizado do Google Drive às {datetime.now().strftime('%H:%M:%S')}")
            except Exception as e:
                print(f"[DB] Falha ao baixar do Google Drive: {e}")
                if _db_path is None:
                    raise
        return _db_path


def get_sqlite():
    conn = sqlite3.connect(str(get_db_path()))
    conn.row_factory = sqlite3.Row
    return conn


# ── PostgreSQL — anotações online ─────────────────────────────

def get_pg():
    """Retorna conexão com PostgreSQL. Retorna None se não configurado."""
    if not DATABASE_URL:
        return None
    try:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
        return conn
    except Exception as e:
        print(f"[PG] Falha ao conectar: {e}")
        return None


def _init_pg():
    """Cria tabela de anotações online no PostgreSQL se não existir."""
    pg = get_pg()
    if not pg:
        return
    try:
        with pg.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS anotacoes_online (
                    id          SERIAL PRIMARY KEY,
                    candidato_id TEXT NOT NULL,
                    texto       TEXT NOT NULL,
                    autor       TEXT,
                    criado_em   TEXT,
                    origem      TEXT DEFAULT 'online',
                    hash_dedup  TEXT UNIQUE
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ao_cid ON anotacoes_online(candidato_id)")
        pg.commit()
        print("[PG] Tabela anotacoes_online pronta.")
    except Exception as e:
        print(f"[PG] Erro ao inicializar tabela: {e}")
    finally:
        pg.close()


def _hash_anotacao(candidato_id, texto, criado_em=""):
    """Gera chave de deduplicação: candidato + primeiros 80 chars do texto + data."""
    import hashlib
    raw = f"{candidato_id}|{texto[:80].strip().lower()}|{str(criado_em)[:10]}"
    return hashlib.md5(raw.encode()).hexdigest()


def pg_salvar_anotacao(candidato_id: str, texto: str, autor: str, criado_em: str):
    """Salva anotação feita online no PostgreSQL."""
    pg = get_pg()
    if not pg:
        return False
    try:
        h = _hash_anotacao(candidato_id, texto, criado_em)
        with pg.cursor() as cur:
            cur.execute("""
                INSERT INTO anotacoes_online (candidato_id, texto, autor, criado_em, origem, hash_dedup)
                VALUES (%s, %s, %s, %s, 'online', %s)
                ON CONFLICT (hash_dedup) DO NOTHING
            """, (str(candidato_id), texto, autor, criado_em, h))
        pg.commit()
        return True
    except Exception as e:
        print(f"[PG] Erro ao salvar anotação: {e}")
        return False
    finally:
        pg.close()


def pg_buscar_anotacoes(candidato_id: str) -> list:
    """Busca anotações online do PostgreSQL para um candidato."""
    pg = get_pg()
    if not pg:
        return []
    try:
        with pg.cursor() as cur:
            cur.execute(
                "SELECT * FROM anotacoes_online WHERE candidato_id=%s ORDER BY criado_em DESC",
                (str(candidato_id),)
            )
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        print(f"[PG] Erro ao buscar anotações: {e}")
        return []
    finally:
        pg.close()


def mesclar_anotacoes(candidato_id: str, anotacoes_sqlite: list) -> list:
    """
    Mescla anotações do SQLite (planilha) com as do PostgreSQL (online).
    Remove duplicatas usando hash de deduplicação.
    Retorna lista ordenada por data decrescente, com campo 'origem' em cada item.
    """
    vistas = set()
    resultado = []

    # Primeiro: anotações online (PostgreSQL) — maior prioridade
    for a in pg_buscar_anotacoes(candidato_id):
        h = a.get("hash_dedup") or _hash_anotacao(candidato_id, a.get("texto",""), a.get("criado_em",""))
        if h not in vistas:
            vistas.add(h)
            a.setdefault("origem", "online")
            resultado.append(a)

    # Depois: anotações do SQLite (planilha/importação)
    for a in anotacoes_sqlite:
        texto     = a.get("texto", "")
        criado_em = a.get("criado_em", "")
        h = _hash_anotacao(candidato_id, texto, criado_em)
        if h not in vistas:
            vistas.add(h)
            d = dict(a)
            d.setdefault("origem", "planilha")
            resultado.append(d)

    # Ordena por data decrescente
    resultado.sort(key=lambda x: x.get("criado_em", ""), reverse=True)
    return resultado


def rows_to_list(rows):
    return [dict(r) for r in rows]


# ── Rotas: painel ─────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "painel.html")


@app.route("/api/status_banco")
def status_banco():
    return jsonify({
        "ultimo_fetch": datetime.fromtimestamp(_last_fetch).strftime("%d/%m/%Y %H:%M:%S") if _last_fetch else "nunca",
        "proximo_fetch_em_segundos": max(0, int(REFRESH_INTERVAL - (time.time() - _last_fetch))) if _last_fetch else 0,
        "gdrive_configurado": bool(GDRIVE_URL and GDRIVE_URL != "COLE_O_LINK_AQUI"),
        "postgres_configurado": bool(DATABASE_URL),
    })


@app.route("/api/forcar_atualizacao", methods=["POST"])
def forcar_atualizacao():
    global _last_fetch
    _last_fetch = 0
    try:
        get_db_path()
        return jsonify({"ok": True, "mensagem": "Banco atualizado com sucesso."})
    except Exception as e:
        return jsonify({"ok": False, "mensagem": str(e)}), 500


# ── Rotas: dados ──────────────────────────────────────────────

@app.route("/api/semestres")
def semestres():
    conn = get_sqlite()
    rows = conn.execute("SELECT DISTINCT semestre FROM inscricoes ORDER BY semestre DESC").fetchall()
    conn.close()
    return jsonify([r["semestre"] for r in rows])


@app.route("/api/polos")
def polos():
    sem = request.args.get("semestre", "")
    conn = get_sqlite()
    q, params = "SELECT DISTINCT polo FROM inscricoes WHERE polo IS NOT NULL", []
    if sem:
        q += " AND semestre=?"; params.append(sem)
    q += " ORDER BY polo"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return jsonify([r["polo"] for r in rows if r["polo"]])


@app.route("/api/dashboard")
def dashboard():
    sem  = request.args.get("semestre", "")
    polo = request.args.get("polo", "")
    meus = request.args.get("meus", "0")
    conn = get_sqlite()
    where, params = _filtros_base(sem, polo, meus)
    total     = conn.execute(f"SELECT COUNT(*) FROM inscricoes {where}", params).fetchone()[0]
    sem_conta = conn.execute(f"SELECT COUNT(*) FROM inscricoes {where} AND (dias_sem_contato IS NULL OR CAST(dias_sem_contato AS INT) > 7)", params).fetchone()[0]
    boleto_av = conn.execute(f"SELECT COUNT(*) FROM inscricoes {where} AND mensalidade LIKE '%VENCER%'", params).fetchone()[0]
    desistiu  = conn.execute(f"SELECT COUNT(*) FROM inscricoes {where} AND (desistiu='SIM' OR cancelou='SIM')", params).fetchone()[0]
    por_status = rows_to_list(conn.execute(f"SELECT status, COUNT(*) as total FROM inscricoes {where} GROUP BY status ORDER BY total DESC LIMIT 10", params).fetchall())
    por_polo   = rows_to_list(conn.execute(f"SELECT polo, COUNT(*) as total FROM inscricoes {where} GROUP BY polo ORDER BY total DESC LIMIT 10", params).fetchall())
    conn.close()
    return jsonify({"total": total, "sem_contato": sem_conta, "boleto_avencer": boleto_av,
                    "desistiu": desistiu, "por_status": por_status, "por_polo": por_polo})


@app.route("/api/inscricoes")
def inscricoes():
    sem         = request.args.get("semestre", "")
    polo        = request.args.get("polo", "")
    meus        = request.args.get("meus", "0")
    busca       = request.args.get("busca", "")
    status      = request.args.get("status", "")
    ordem       = request.args.get("ordem", "nome")
    direcao     = request.args.get("direcao", "ASC")
    pagina      = int(request.args.get("pagina", 1))
    por_pag     = int(request.args.get("por_pagina", 50))
    painel      = request.args.get("painel", "todos")
    insc_de     = request.args.get("insc_de", "")
    insc_ate    = request.args.get("insc_ate", "")
    contato_de  = request.args.get("contato_de", "")
    contato_ate = request.args.get("contato_ate", "")

    conn = get_sqlite()
    where, params = _filtros_base(sem, polo, meus)

    if busca:
        where += " AND (nome LIKE ? OR candidato LIKE ? OR cpf LIKE ? OR email LIKE ?)"
        b = f"%{busca}%"; params += [b, b, b, b]
    if status:
        where += " AND status=?"; params.append(status)
    if insc_de:
        where += " AND dt_inscricao >= ?"; params.append(insc_de)
    if insc_ate:
        where += " AND dt_inscricao <= ?"; params.append(insc_ate)
    if contato_de:
        where += " AND ultimo_contato >= ?"; params.append(contato_de)
    if contato_ate:
        where += " AND ultimo_contato <= ?"; params.append(contato_ate)
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
    if ordem not in colunas_validas: ordem = "nome"
    if direcao not in ("ASC","DESC"): direcao = "ASC"

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
    conn = get_sqlite()
    row  = conn.execute("SELECT * FROM inscricoes WHERE id=?", (iid,)).fetchone()
    anotacoes_sqlite = rows_to_list(conn.execute(
        "SELECT * FROM anotacoes WHERE candidato_id=? ORDER BY criado_em DESC", (str(iid),)
    ).fetchall())
    conn.close()
    if not row:
        return jsonify({"erro": "não encontrado"}), 404

    anotacoes = mesclar_anotacoes(str(iid), anotacoes_sqlite)
    return jsonify({"inscricao": dict(row), "anotacoes": anotacoes})


@app.route("/api/inscricoes/<int:iid>/anotacao", methods=["POST"])
def add_anotacao(iid):
    data  = request.json
    texto = data.get("texto", "").strip()
    autor = data.get("autor", MEU_NOME)
    if not texto:
        return jsonify({"erro": "texto vazio"}), 400

    criado_em = datetime.now().isoformat()

    pg_ok = pg_salvar_anotacao(str(iid), texto, autor, criado_em)

    if not pg_ok:
        try:
            conn = get_sqlite()
            conn.execute(
                "INSERT INTO anotacoes (candidato_id, texto, autor, criado_em) VALUES (?,?,?,?)",
                (str(iid), texto, autor, criado_em)
            )
            conn.execute(
                "UPDATE inscricoes SET anotacoes=? WHERE id=?",
                (f"[{datetime.now().strftime('%d/%m/%y')}] {texto}", iid)
            )
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[SQLite] Erro ao salvar anotação local: {e}")
            return jsonify({"erro": "Falha ao salvar anotação"}), 500

    return jsonify({"ok": True, "destino": "postgres" if pg_ok else "sqlite_local"})


@app.route("/api/status_lista")
def status_lista():
    sem = request.args.get("semestre", "")
    conn = get_sqlite()
    q, params = "SELECT DISTINCT status FROM inscricoes WHERE status IS NOT NULL", []
    if sem:
        q += " AND semestre=?"; params.append(sem)
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return jsonify([r["status"] for r in rows if r["status"]])


@app.route("/api/exportar")
def exportar():
    sem    = request.args.get("semestre", "")
    polo   = request.args.get("polo", "")
    meus   = request.args.get("meus", "0")
    busca  = request.args.get("busca", "")
    status = request.args.get("status", "")
    painel = request.args.get("painel", "todos")

    conn = get_sqlite()
    where, params = _filtros_base(sem, polo, meus)
    if busca:
        where += " AND (nome LIKE ? OR candidato LIKE ? OR cpf LIKE ?)"
        b = f"%{busca}%"; params += [b, b, b]
    if status:
        where += " AND status=?"; params.append(status)
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

    cols  = rows[0].keys()
    lines = [";".join(cols)] + [";".join(str(r[c] or "") for c in cols) for r in rows]
    return Response(
        "\n".join(lines),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=crm_export_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"}
    )


def _filtros_base(sem, polo, meus):
    where, params = "WHERE 1=1", []
    if sem:
        where += " AND semestre=?"; params.append(sem)
    if polo:
        where += " AND polo=?"; params.append(polo)
    if meus == "1":
        like = f"%{MEU_NOME}%"
        where += " AND (inscrito_por LIKE ? OR boleto_gerado_por LIKE ? OR resp_ultimo_atendimento LIKE ?)"
        params += [like, like, like]
    return where, params


# ── Inicialização ──────────────────────────────────────────────

_init_pg()

if __name__ == "__main__":
    print("=" * 55)
    print("  CRM Unicesumar — Servidor iniciado")
    print("  Acesse: http://localhost:5000")
    print(f"  Banco SQLite : {'Google Drive' if GDRIVE_URL != 'COLE_O_LINK_AQUI' else 'arquivo local'}")
    print(f"  Anotações    : {'PostgreSQL (Railway)' if DATABASE_URL else 'SQLite local (fallback)'}")
    print("  Ctrl+C para parar")
    print("=" * 55)
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
