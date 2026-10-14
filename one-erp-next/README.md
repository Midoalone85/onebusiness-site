# ONE ERP Next: independent foundation

A standalone prototype of tenant access rules and seven-day trial policies. This is independent of Odoo, but **not yet a deployed or production-ready ERP**. The current website, Oracle server and Odoo instance are not changed.

Test locally with Python >=3.11:

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

See ARCHITECTURE.md for what is implemented and what is not.
