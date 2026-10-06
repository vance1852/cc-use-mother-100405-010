"""科技战略协作基础服务的服务端基础包。"""

from .gov_service import GovernanceService
from .service import DomainService

__all__ = ["DomainService", "GovernanceService"]
