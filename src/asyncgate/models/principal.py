"""Principal model - actors in the system."""


from pydantic import BaseModel

from asyncgate.models.enums import PrincipalKind


class Principal(BaseModel):
    """Represents an actor in the system (agent, worker, service, etc.)."""

    kind: PrincipalKind
    id: str
    instance_id: str | None = None

    def __hash__(self) -> int:
        return hash((self.kind, self.id, self.instance_id))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Principal):
            return False
        return (
            self.kind == other.kind
            and self.id == other.id
            and self.instance_id == other.instance_id
        )
