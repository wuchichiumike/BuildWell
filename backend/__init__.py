"""筑福链 backend package.

The package deliberately keeps the business service independent from the HTTP
adapter.  SQLite is used by default for the demo; a PostgreSQL repository can
implement the same methods without changing the domain calculations.
"""

from .config import Settings
from .db import Database
from .service import BusinessService, DomainError

__all__ = ["BusinessService", "Database", "DomainError", "Settings"]
