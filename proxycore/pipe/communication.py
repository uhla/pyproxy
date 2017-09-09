import asyncio
import logging
import traceback

from typing import Union, Iterable

from proxycore.parser.http_parser import HttpMessage, HttpResponse
from proxycore.pipe.endpoint import Endpoint, InputEndpoint, OutputEndpoint

logger = logging.getLogger(__name__)

class FlowDefinition:
    def endpoints(self) -> Iterable[Endpoint]:
        return ()

    def get_flow(self, endpoint_name):
        return self.default_flow

    def default_flow(self, request):
        yield from []

    def reset(self):
        pass


class Dispatcher:
    def __init__(self, flow_definition: FlowDefinition):
        self.endpoints = {}
        self.flow_definition = flow_definition
        for endpoint in flow_definition.endpoints():
            self.add_endpoint(endpoint)

    def add_endpoint(self, endpoint: Endpoint):
        endpoint.dispatcher = self
        self.endpoints[endpoint.name] = endpoint

    async def dispatch(self, source_endpoint: Union[str, Endpoint], received_message: HttpMessage):
        if isinstance(source_endpoint, str):
            source_endpoint = self.endpoints[source_endpoint]

        processing, target_endpoint, message_to_send = await source_endpoint.on_received(received_message)

        if isinstance(target_endpoint, str):
            target_endpoint = self.endpoints[target_endpoint]

        await target_endpoint.send(message_to_send, processing)

    async def handle_client(self, endpoint_name, reader, writer):
        try:
            flow = self.flow_definition.get_flow(endpoint_name)
            await self.endpoints[endpoint_name].connection_opened(reader, writer, flow)
            for endpoint in self.endpoints.values():
                if isinstance(endpoint, OutputEndpoint):
                    await endpoint.open_connection()

            await self.loop()
        except Exception as e:
            trace = traceback.format_exception(e.__class__, e, e.__traceback__)
            trace = "".join(trace)
            logger.error(trace)

            header = "Internal proxy error:\n"
            header += str(e) + "\n\n"

            response = HttpResponse(b"500", b"Internal proxy error",
                                    body=(header + trace).encode())

            for b in response.to_bytes():
                writer.write(b)
            await writer.drain()

    async def loop(self):
        try:
            futures = []
            for endpoint in self.endpoints.values():
                futures.append(asyncio.ensure_future(self.__loop1(endpoint)))
            await asyncio.gather(*futures, return_exceptions=True)
        finally:
            for endpoint in self.endpoints.values():
                endpoint.close()

    async def __loop1(self, endpoint):
        async def _dispatch(message):
            await self.dispatch(endpoint, message)

        await endpoint.read_loop(_dispatch)


class Server:
    def __init__(self, flow_definition: FlowDefinition):
        self.flow_definition = flow_definition
        self.servers = []

    async def start(self):
        self.flow_definition.reset()
        endpoints = list(self.flow_definition.endpoints())
        try:
            self.servers = []
            for endpoint in endpoints:
                if isinstance(endpoint, InputEndpoint):
                    future = await self.__start1(endpoint)
                    self.servers.append(future)
        except Exception as e:
            print("Cannot start server: {}".format(e))

            self.servers = []

    async def __start1(self, endpoint: InputEndpoint):
        async def _handle_client(reader, writer):
            await self.handle_client(reader, writer, endpoint.name)
        return await endpoint.listen(_handle_client)

    async def handle_client(self, reader, writer, endpoint_name):
        dispatcher = Dispatcher(self.flow_definition)
        await dispatcher.handle_client(endpoint_name, reader, writer)

    async def close(self, wait_closed=False):
        try:
            for future in self.servers:
                future.close()

            if wait_closed:
                for future in self.servers:
                    await future.wait_closed()
        except Exception as e:
            print("Error closing the server: {}".format(e))
