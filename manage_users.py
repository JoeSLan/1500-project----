"""Admin CLI for LegalSite Auditor accounts (edits users.json and exports Streamlit secrets).

Examples:
    python manage_users.py add cliente@correo.com --plan mensual --name "Inmobiliaria X"
    python manage_users.py add socio@correo.com --plan vitalicio
    python manage_users.py renew cliente@correo.com            # +1 periodo de su plan
    python manage_users.py renew cliente@correo.com --plan anual
    python manage_users.py set-exp cliente@correo.com 2027-01-31
    python manage_users.py deactivate cliente@correo.com       # dar de baja
    python manage_users.py activate cliente@correo.com
    python manage_users.py passwd cliente@correo.com
    python manage_users.py list
    python manage_users.py export-toml > secrets_users.toml    # pegar en Streamlit Cloud → Secrets
    python manage_users.py hash                                 # solo generar un password_hash
"""

from __future__ import annotations

import argparse
import calendar
import getpass
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

from auditor.auth import (
    PLAN_LABELS,
    PLAN_TYPES,
    User,
    UserConfigError,
    evaluate_membership,
    hash_password,
    normalize_email,
    read_users_json,
    today_mx,
)

USERS_FILE = Path(__file__).resolve().parent / "users.json"


# ------------------------------------------------------------------ helpers
def add_months(start: date, months: int) -> date:
    """Same day N months later, clamped to the month's last day (31 ene + 1 mes = 28/29 feb)."""
    month_index = start.month - 1 + months
    year, month = start.year + month_index // 12, month_index % 12 + 1
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


def next_expiration(plan: str, current: date | None, today: date) -> date | None:
    """Extend from the later of today or the current expiration, so early renewals don't lose days."""
    if plan == "vitalicio":
        return None
    base = max(today, current) if current else today
    return add_months(base, 1 if plan == "mensual" else 12)


def load(path: Path) -> list[dict[str, Any]]:
    return read_users_json(path)


def save(path: Path, records: list[dict[str, Any]]) -> None:
    for record in records:  # validate before writing anything
        User.from_record(record)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"users": records}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def find(records: list[dict[str, Any]], email: str) -> dict[str, Any]:
    email = normalize_email(email)
    for record in records:
        if normalize_email(str(record.get("email", ""))) == email:
            return record
    raise SystemExit(f"✗ No existe la cuenta {email}")


def ask_password() -> str:
    first = getpass.getpass("Contraseña: ")
    if len(first) < 8:
        raise SystemExit("✗ La contraseña debe tener al menos 8 caracteres.")
    if getpass.getpass("Confirma la contraseña: ") != first:
        raise SystemExit("✗ Las contraseñas no coinciden.")
    return first


def describe(record: dict[str, Any]) -> str:
    user = User.from_record(record)
    status = evaluate_membership(user)
    if status.days_remaining is None and status.allowed:
        state = "vitalicio"
    elif status.allowed:
        state = f"{status.days_remaining} días (vence {user.exp_date:%Y-%m-%d})"
    else:
        state = status.code.lower()
    return f"{user.email:<35} {PLAN_LABELS[user.plan_type]:<10} {'activo' if user.is_active else 'BAJA':<7} {state}"


def toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return json.dumps(value, ensure_ascii=False)  # JSON strings are valid TOML basic strings


# ------------------------------------------------------------------ commands
def cmd_add(args: argparse.Namespace) -> None:
    records = load(args.file)
    email = normalize_email(args.email)
    if any(normalize_email(str(r.get("email", ""))) == email for r in records):
        raise SystemExit(f"✗ {email} ya existe. Usa 'renew', 'set-exp' o 'passwd'.")
    exp = date.fromisoformat(args.exp) if args.exp else next_expiration(args.plan, None, today_mx())
    record: dict[str, Any] = {
        "email": email,
        "name": args.name or "",
        "password_hash": hash_password(ask_password()),
        "plan_type": args.plan,
        "exp_date": exp.isoformat() if exp else "",
        "is_active": True,
        "is_lifetime": args.plan == "vitalicio",
    }
    records.append(record)
    save(args.file, records)
    print(f"✓ Cuenta creada: {describe(record)}")


def cmd_renew(args: argparse.Namespace) -> None:
    records = load(args.file)
    record = find(records, args.email)
    plan = args.plan or record["plan_type"]
    current = User.from_record(record).exp_date
    exp = next_expiration(plan, current, today_mx())
    record.update(plan_type=plan, exp_date=exp.isoformat() if exp else "", is_lifetime=plan == "vitalicio", is_active=True)
    save(args.file, records)
    print(f"✓ Renovada: {describe(record)}")


def cmd_set_exp(args: argparse.Namespace) -> None:
    records = load(args.file)
    record = find(records, args.email)
    record["exp_date"] = date.fromisoformat(args.date).isoformat()
    save(args.file, records)
    print(f"✓ Actualizada: {describe(record)}")


def cmd_set_active(args: argparse.Namespace, active: bool) -> None:
    records = load(args.file)
    record = find(records, args.email)
    record["is_active"] = active
    save(args.file, records)
    print(f"✓ {'Activada' if active else 'Dada de baja'}: {describe(record)}")


def cmd_passwd(args: argparse.Namespace) -> None:
    records = load(args.file)
    record = find(records, args.email)
    record["password_hash"] = hash_password(ask_password())
    record.pop("password", None)
    save(args.file, records)
    print(f"✓ Contraseña actualizada para {record['email']}")


def cmd_list(args: argparse.Namespace) -> None:
    records = load(args.file)
    if not records:
        print(f"No hay cuentas en {args.file.name}.")
    for record in records:
        print(describe(record))


def cmd_export_toml(args: argparse.Namespace) -> None:
    fields = ("email", "name", "password_hash", "password", "plan_type", "exp_date", "is_active", "is_lifetime")
    print("# Pega esto en Streamlit Cloud → tu app → Settings → Secrets (junto a ANTHROPIC_API_KEY)")
    for record in load(args.file):
        print("\n[[users]]")
        for key in fields:
            if key in record and record[key] not in (None, ""):
                print(f"{key} = {toml_value(record[key])}")


def cmd_hash(_: argparse.Namespace) -> None:
    print(hash_password(ask_password()))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Administración de cuentas de LegalSite Auditor.")
    parser.add_argument("--file", type=Path, default=USERS_FILE, help="Ruta a users.json")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("add", help="Crear cuenta")
    p.add_argument("email")
    p.add_argument("--plan", choices=PLAN_TYPES, required=True)
    p.add_argument("--exp", help="Fecha de vencimiento YYYY-MM-DD (default: +1 mes / +1 año)")
    p.add_argument("--name", help="Nombre o empresa")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("renew", help="Extender un periodo (desde hoy o desde su vencimiento, lo que sea mayor)")
    p.add_argument("email")
    p.add_argument("--plan", choices=PLAN_TYPES)
    p.set_defaults(func=cmd_renew)

    p = sub.add_parser("set-exp", help="Fijar fecha de vencimiento exacta")
    p.add_argument("email")
    p.add_argument("date", help="YYYY-MM-DD")
    p.set_defaults(func=cmd_set_exp)

    p = sub.add_parser("deactivate", help="Dar de baja (bloquea el acceso de inmediato)")
    p.add_argument("email")
    p.set_defaults(func=lambda a: cmd_set_active(a, False))

    p = sub.add_parser("activate", help="Reactivar cuenta")
    p.add_argument("email")
    p.set_defaults(func=lambda a: cmd_set_active(a, True))

    p = sub.add_parser("passwd", help="Cambiar contraseña")
    p.add_argument("email")
    p.set_defaults(func=cmd_passwd)

    sub.add_parser("list", help="Listar cuentas").set_defaults(func=cmd_list)
    sub.add_parser("export-toml", help="Exportar cuentas en formato Streamlit Secrets").set_defaults(func=cmd_export_toml)
    sub.add_parser("hash", help="Generar un password_hash").set_defaults(func=cmd_hash)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except (UserConfigError, ValueError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
