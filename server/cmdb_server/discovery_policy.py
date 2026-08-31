"""Administrator-owned scan policy. Agent reports never update this policy."""
import ipaddress

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ScanPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool = False
    auto_subnets: bool = True
    cidrs: list[str] = Field(default_factory=list, max_length=32)
    interval_seconds: int = Field(default=86400, ge=3600, le=604800)
    max_hosts: int = Field(default=1024, ge=1, le=4096)
    rate: int = Field(default=32, ge=1, le=128)
    budget_seconds: int = Field(default=300, ge=10, le=900)

    @model_validator(mode="after")
    def validate_ranges(self):
        private = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
        networks = []
        for cidr in self.cidrs:
            if len(cidr) > 64:
                raise ValueError("zbyt dlugi CIDR")
            network = ipaddress.ip_network(cidr, strict=False)
            if network.version != 4 or not any(network.subnet_of(n) for n in private):
                raise ValueError("dozwolone sa tylko prywatne podsieci IPv4")
            networks.append(network)
        networks = list(ipaddress.collapse_addresses(networks))
        if sum(n.num_addresses if n.prefixlen >= 31 else n.num_addresses - 2 for n in networks) > self.max_hosts:
            raise ValueError("zakres przekracza limit hostow")
        if self.enabled and not self.auto_subnets and not networks:
            raise ValueError("podaj CIDR lub wlacz automatyczne podsieci")
        self.cidrs = [str(n) for n in networks]
        return self
