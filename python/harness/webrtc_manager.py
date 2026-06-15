import asyncio
import json
import logging
import uuid
from typing import Any, Callable, Coroutine

from aiortc import RTCPeerConnection, RTCSessionDescription, RTCDataChannel, RTCConfiguration, RTCIceServer

logger = logging.getLogger(__name__)

class WebRTCManager:
    def __init__(self):
        # Maps peer_username to RTCPeerConnection
        self.connections: dict[str, RTCPeerConnection] = {}
        # Maps peer_username to the primary RTCDataChannel
        self.data_channels: dict[str, RTCDataChannel] = {}
        # Message routing callback
        self.on_message_callback: Callable[[str, dict], Coroutine] = None
        # Queues for pending RPC responses across data channels
        self.pending_rpcs: dict[str, asyncio.Future] = {}
        
    def set_message_handler(self, callback: Callable[[str, dict], Coroutine]):
        self.on_message_callback = callback

    def _create_rtc_config(self) -> RTCConfiguration:
        return RTCConfiguration(
            iceServers=[
                RTCIceServer(urls=["stun:stun.l.google.com:19302"])
            ]
        )

    async def create_offer(self, peer_username: str) -> dict:
        """Create an RTCPeerConnection and generate an SDP offer for the peer."""
        pc = RTCPeerConnection(configuration=self._create_rtc_config())
        self.connections[peer_username] = pc
        
        channel = pc.createDataChannel("tala-rpc")
        self._setup_channel(peer_username, channel)
        
        @pc.on("iceconnectionstatechange")
        async def on_iceconnectionstatechange():
            logger.info("WebRTC ICE state for %s is %s", peer_username, pc.iceConnectionState)
            if pc.iceConnectionState in ["failed", "closed"]:
                await self.close_connection(peer_username)

        await pc.setLocalDescription(await pc.createOffer())
        
        return {
            "sdp": pc.localDescription.sdp,
            "type": pc.localDescription.type
        }

    async def handle_offer(self, peer_username: str, offer: dict) -> dict:
        """Handle an incoming SDP offer and return an SDP answer."""
        pc = RTCPeerConnection(configuration=self._create_rtc_config())
        self.connections[peer_username] = pc

        @pc.on("datachannel")
        def on_datachannel(channel):
            self._setup_channel(peer_username, channel)

        @pc.on("iceconnectionstatechange")
        async def on_iceconnectionstatechange():
            logger.info("WebRTC ICE state for %s is %s", peer_username, pc.iceConnectionState)
            if pc.iceConnectionState in ["failed", "closed"]:
                await self.close_connection(peer_username)

        desc = RTCSessionDescription(sdp=offer["sdp"], type=offer["type"])
        await pc.setRemoteDescription(desc)
        await pc.setLocalDescription(await pc.createAnswer())

        return {
            "sdp": pc.localDescription.sdp,
            "type": pc.localDescription.type
        }

    async def handle_answer(self, peer_username: str, answer: dict):
        """Handle an incoming SDP answer."""
        pc = self.connections.get(peer_username)
        if not pc:
            logger.warning("Received answer for unknown peer: %s", peer_username)
            return
        
        desc = RTCSessionDescription(sdp=answer["sdp"], type=answer["type"])
        await pc.setRemoteDescription(desc)

    def _setup_channel(self, peer_username: str, channel: RTCDataChannel):
        @channel.on("open")
        def on_open():
            logger.info("WebRTC Data Channel open for %s", peer_username)
            self.data_channels[peer_username] = channel

        @channel.on("close")
        def on_close():
            logger.info("WebRTC Data Channel closed for %s", peer_username)
            self.data_channels.pop(peer_username, None)

        @channel.on("message")
        def on_message(message):
            try:
                data = json.loads(message)
            except Exception as e:
                logger.error("Failed to parse WebRTC message from %s: %s", peer_username, e)
                return

            # Check if this is a response to an RPC we sent
            req_id = data.get("id")
            if req_id and req_id in self.pending_rpcs and "method" not in data:
                # It's a response
                future = self.pending_rpcs.pop(req_id)
                if not future.done():
                    future.set_result(data)
                return

            # Otherwise, it's an incoming request or notification
            if self.on_message_callback:
                asyncio.create_task(self.on_message_callback(peer_username, data))

    async def send_rpc(self, peer_username: str, method: str, params: dict) -> dict:
        """Send a JSON-RPC request over the data channel and wait for response."""
        channel = self.data_channels.get(peer_username)
        if not channel or channel.readyState != "open":
            raise RuntimeError(f"No open WebRTC data channel for {peer_username}")

        req_id = str(uuid.uuid4())
        request = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params
        }
        
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self.pending_rpcs[req_id] = future
        
        channel.send(json.dumps(request))
        
        # Wait for the response with timeout
        try:
            response = await asyncio.wait_for(future, timeout=15.0)
        except asyncio.TimeoutError:
            self.pending_rpcs.pop(req_id, None)
            raise RuntimeError(f"WebRTC RPC timeout for {method} to {peer_username}")
            
        if "error" in response:
            raise RuntimeError(response["error"].get("message", response["error"]))
            
        result = response.get("result", {})
        return result if isinstance(result, dict) else {"result": result}

    async def close_connection(self, peer_username: str):
        self.data_channels.pop(peer_username, None)
        pc = self.connections.pop(peer_username, None)
        if pc:
            await pc.close()

webrtc_manager = WebRTCManager()
