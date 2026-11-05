"""
app/kernel/__init__.py

The kernel package contains ALL domain logic.
It MUST NOT import from fastapi, aiogram, or any HTTP/bot framework.
This constraint is enforced by tests/kernel/test_kernel_isolation.py.
"""
