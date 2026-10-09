/** Incremental SSE decoder. A UTF-8 decoder must run before feeding text chunks. */
export function createSSEParser(onData: (data: string) => void) {
  let pending = '';
  let data: string[] = [];

  function dispatch() {
    if (data.length) {
      const value = data.join('\n');
      data = [];
      onData(value);
    }
  }

  function consumeLine(line: string) {
    if (line === '') {
      dispatch();
      return;
    }
    if (line.startsWith(':')) return;
    const separator = line.indexOf(':');
    const field = separator < 0 ? line : line.slice(0, separator);
    let value = separator < 0 ? '' : line.slice(separator + 1);
    if (value.startsWith(' ')) value = value.slice(1);
    if (field === 'data') data.push(value);
  }

  return {
    push(chunk: string) {
      pending += chunk;
      let newline: number;
      while ((newline = pending.indexOf('\n')) >= 0) {
        consumeLine(pending.slice(0, newline).replace(/\r$/, ''));
        pending = pending.slice(newline + 1);
      }
    },
    finish() {
      if (pending) consumeLine(pending.replace(/\r$/, ''));
      pending = '';
      dispatch();
    },
  };
}

export async function readSSE<T>(body: ReadableStream<Uint8Array>, onEvent: (event: T) => void) {
  const decoder = new TextDecoder();
  const parser = createSSEParser((data) => {
    let event: T;
    try {
      event = JSON.parse(data) as T;
    } catch {
      throw new Error('收到的进度消息无法解析，请重新载入会话查看执行结果。');
    }
    onEvent(event);
  });
  const reader = body.getReader();
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      parser.push(decoder.decode(value, { stream: true }));
    }
    parser.push(decoder.decode());
    parser.finish();
  } finally {
    reader.releaseLock();
  }
}
