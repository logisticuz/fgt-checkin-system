"""Explicit provider substitutes; none of these objects opens a network connection."""

import inspect
import json
from dataclasses import dataclass, field

import httpx


@dataclass
class HTTPStub:
    response: dict = field(default_factory=dict)
    status_code: int = 200
    calls: list = field(default_factory=list)
    on_call: object = None

    def record(self, request):
        payload = json.loads(request.content) if request.content else None
        self.calls.append({"method": request.method, "url": str(request.url), "json": payload})

    def handle(self, request):
        self.record(request)
        if self.on_call is not None:
            self.on_call(request)
        return httpx.Response(self.status_code, json=self.response, request=request)

    async def handle_async(self, request):
        self.record(request)
        if self.on_call is not None:
            result = self.on_call(request)
            if inspect.isawaitable(result):
                await result
        return httpx.Response(self.status_code, json=self.response, request=request)


class N8NStub(HTTPStub):
    pass


class EbasStub(HTTPStub):
    pass


class StartGGStub(HTTPStub):
    pass


@dataclass
class SMTPStub:
    calls: list = field(default_factory=list)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def starttls(self, *args, **kwargs):
        return 220, b"test TLS ready"

    def login(self, *args, **kwargs):
        return 235, b"test authenticated"

    def sendmail(self, from_addr, to_addrs, msg, **kwargs):
        self.calls.append({"from": from_addr, "to": to_addrs, "message": msg})
        return {}

    def send_message(self, msg, from_addr=None, to_addrs=None, **kwargs):
        return self.sendmail(from_addr or msg["From"], to_addrs or msg["To"], msg)

    def quit(self):
        return 221, b"test closed"
