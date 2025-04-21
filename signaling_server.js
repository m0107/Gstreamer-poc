const WebSocket = require('ws');

const PORT = 8765;
const wss = new WebSocket.Server({ port: PORT });
const clients = new Map(); // Store clients with stream_id

wss.on('connection', (ws, req) => {
    const clientAddress = req.socket.remoteAddress;
    console.log(`[INFO] New client connected: ${clientAddress}`);

    ws.on('message', (message) => {
        console.log(`[DEBUG] Received message from ${clientAddress}: ${message}`);
        let parsedMsg;
        try {
            parsedMsg = JSON.parse(message);
        } catch (err) {
            console.error("[ERROR] Invalid JSON message:", message);
            return;
        }

        const streamId = parsedMsg.stream_id;
        if (!streamId) {
            console.error("[ERROR] Missing stream_id in message");
            return;
        }

        if (parsedMsg.type === "register") {
            // Register client with a stream_id
            clients.set(ws, streamId);
            console.log(`[INFO] Client ${clientAddress} registered for stream ${streamId}`);
            return;
        }

        // Forward message to other clients with the same stream_id
        clients.forEach((otherStreamId, client) => {
            if (client !== ws && client.readyState === WebSocket.OPEN && otherStreamId === streamId) {
                console.log(`[DEBUG] Forwarding message for stream ${streamId} to a client`);
                client.send(message);
            }
        });
    });

    ws.on('close', () => {
        clients.delete(ws);
        console.log(`[INFO] Client disconnected: ${clientAddress}`);
    });

    ws.on('error', (err) => {
        console.error(`[ERROR] Client error from ${clientAddress}: ${err}`);
    });
});

console.log(`Signaling server started on port ${PORT}`);
