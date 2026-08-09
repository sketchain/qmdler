// WebSocket 客户端：自动重连 + 心跳。
// 服务端在连上时先推一帧全量快照，再补最近的历史事件，最后才是增量。

export function connect(handlers) {
  let socket = null;
  let heartbeat = null;
  let retry = 0;
  let closed = false;

  function scheduleReconnect() {
    if (closed) return;
    retry += 1;
    const delay = Math.min(30000, 1000 * 2 ** Math.min(retry, 5));
    setTimeout(open, delay);
  }

  function open() {
    if (closed) return;
    const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
    socket = new WebSocket(`${scheme}://${location.host}/ws`);

    socket.onopen = () => {
      retry = 0;
      handlers.onOpen && handlers.onOpen();
      heartbeat = setInterval(() => {
        if (socket && socket.readyState === WebSocket.OPEN) socket.send('ping');
      }, 25000);
    };

    socket.onmessage = (event) => {
      let frame;
      try {
        frame = JSON.parse(event.data);
      } catch (_) {
        return;
      }
      handlers.onEvent && handlers.onEvent(frame);
    };

    socket.onclose = () => {
      clearInterval(heartbeat);
      handlers.onClose && handlers.onClose();
      scheduleReconnect();
    };

    socket.onerror = () => {
      if (socket) socket.close();
    };
  }

  open();

  return {
    close() {
      closed = true;
      clearInterval(heartbeat);
      if (socket) socket.close();
    },
  };
}
