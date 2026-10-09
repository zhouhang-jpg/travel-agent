// Preload guard for the unmodified official FlyAI CLI. No requests are rewritten.
// Its immediate process.exit(0) after fetch can abort Windows libuv on Node 24.
// A sentinel stops that command at exactly the same point; pending native handles
// can then close naturally. Only explicit zero exits are intercepted.
const os = require('node:os');
const fs = require('node:fs');
const path = require('node:path');
const stateDir = process.env.FLYAI_STATE_DIR;
if (!stateDir || !path.isAbsolute(stateDir)) {
  throw new Error('FLYAI_STATE_DIR must be an explicit absolute application directory.');
}
fs.mkdirSync(stateDir, { recursive: true });
os.homedir = () => stateDir;
os.tmpdir = () => stateDir;
const successfulExit = Symbol('flyai-successful-exit');
const originalExit = process.exit.bind(process);
let failed = false;
process.exit = (code = 0) => {
  if (Number(code) === 0) throw successfulExit;
  return originalExit(code);
};
function handleExit(error) {
  if (error === successfulExit) {
    if (!failed) process.exitCode = 0;
    return;
  }
  // Do not print arbitrary upstream errors, which may include request credentials.
  process.stderr.write('FlyAI command failed before normal completion.\n');
  failed = true;
  process.exitCode = 1;
}
process.on('unhandledRejection', handleExit);
process.on('uncaughtException', handleExit);
