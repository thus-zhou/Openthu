"""Compatibility entry point for the process-owned campus gateway."""
from .gateway_client import CampusError, GatewayClient, gateway, close_gateway, request_control

__all__ = ['CampusError', 'GatewayClient', 'gateway', 'close_gateway', 'request_control']
