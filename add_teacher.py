#!/usr/bin/env python3
"""Add or update a teacher login for Homework Wizard (run it on the server).

  python3 add_teacher.py                  asks for email, password and confirmation
  python3 add_teacher.py --list           shows the teacher emails
  python3 add_teacher.py --remove EMAIL   deletes a teacher (also logs them out everywhere)

Use the same DATA_DIR (or TEACHERS_FILE) as the server, e.g.  DATA_DIR=./data/jobs python3 add_teacher.py
"""
import getpass, os, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import homework_wizard as hw


def ask_email():
    while True:
        email = hw.norm_email(input("Teacher email: "))
        if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            return email
        print("  That doesn't look like an email address.")

def ask_password():
    while True:
        pw = getpass.getpass(f"Password (at least {hw.MIN_PASSWORD} characters): ")
        if len(pw) < hw.MIN_PASSWORD:
            print(f"  Too short; use at least {hw.MIN_PASSWORD} characters.")
            continue
        if getpass.getpass("Confirm password: ") != pw:
            print("  The passwords don't match, try again.")
            continue
        return pw

def main(args):
    if args == ["--list"]:
        keys = hw.read_keys()
        print("\n".join(f"{m}  (added {r.get('created', '?')})" for m, r in sorted(keys.items())) or "No teachers yet.")
        return
    if len(args) == 2 and args[0] == "--remove":
        keys = hw.read_keys()
        if keys.pop(hw.norm_email(args[1]), None) is None:
            sys.exit(f"No teacher with email {args[1]}.")
        hw.write_keys(keys)
        print(f"Removed {args[1]}; they are logged out everywhere.")
        return
    if args:
        sys.exit(__doc__)
    print(f"Teacher logins are stored in {hw.KEYS}\n")
    email = ask_email()
    if email in hw.read_keys() and input(f"{email} already exists. Change the password? [y/N] ").strip().lower() != "y":
        return print("Nothing changed.")
    hw.set_teacher(email, ask_password())
    print(f"\nDone: {email} can now log in (Teacher login, at the bottom of the page).")


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.")
