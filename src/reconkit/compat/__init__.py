"""Compatibility with the archived ``data-diff`` command line and its dbt integration.

``reconkit data-diff`` accepts data-diff's arguments, connection URIs, ``--conf`` files and
``--dbt`` project settings, and prints data-diff's output formats, so a script that called
``data-diff`` keeps working after replacing the command name. See
docs/migrating-from-data-diff.md for what is and is not carried over.
"""
