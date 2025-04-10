// signaling_server.js
const WebSocket = require('ws');

const PORT = 8765;
const wss = new WebSocket.Server({ port: PORT });
const clients = new Set();

// Global variable to store the most recent offer message.
let lastOfferMessage = null;
wss.on('connection', (ws, req) => {
    const clientAddress = req.socket.remoteAddress;
    console.log(`[INFO] New client connected: ${clientAddress}`);
    clients.add(ws);
  
    // Delay sending the stored offer (if any) by 100ms to ensure the client is ready.
    if (lastOfferMessage) {
      console.log(`[INFO] Sending stored offer to new client ${clientAddress}`);
      setTimeout(() => {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(lastOfferMessage);
        }
      }, 100);
    }
  
    ws.on('message', (message) => {
      console.log(`[DEBUG] Received message from ${clientAddress}: ${message}`);
  
      let parsedMsg;
      try {
        parsedMsg = JSON.parse(message);
      } catch (err) {
        console.error("[ERROR] Invalid JSON message:", message);
        return;
      }
  
      // If this is an offer from the publisher, save it.
      if (parsedMsg.type === "offer") {
        lastOfferMessage = message;
      }
  
      // Broadcast the message to all other connected clients.
      clients.forEach(client => {
        if (client !== ws && client.readyState === WebSocket.OPEN) {
          console.log(`[DEBUG] Forwarding message to a client`);
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
  