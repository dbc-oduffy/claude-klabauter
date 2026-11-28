/** Execute the fleet Workflow body using a JSON-line agent broker on stdin/stdout.
 * This is an executor for trusted emitted scripts, not a JavaScript sandbox.
 * Script stdout is reserved for the broker protocol; console output goes to stderr.
 */
import { readFileSync } from 'node:fs';
import { createInterface } from 'node:readline';
import { pathToFileURL } from 'node:url';
import { resolve } from 'node:path';
import { isDeepStrictEqual } from 'node:util';
import { createHash } from 'node:crypto';

export const MAX_WORKFLOW_DEPTH = 32;

export function workflowBody(source) {
  // Fleet scripts open with metadata. Recognize only the first code token: a
  // quoted/template/comment occurrence of "export" later in the body is untouched.
  let offset = source.charCodeAt(0) === 0xfeff ? 1 : 0;
  while (offset < source.length) {
    const white = /^\s+/.exec(source.slice(offset));
    if (white) { offset += white[0].length; continue; }
    if (source.startsWith('//', offset)) {
      const end = source.indexOf('\n', offset + 2);
      offset = end < 0 ? source.length : end + 1;
      continue;
    }
    if (source.startsWith('/*', offset)) {
      const end = source.indexOf('*/', offset + 2);
      if (end < 0) throw new SyntaxError('Unterminated leading workflow comment');
      offset = end + 2;
      continue;
    }
    break;
  }
  if (!/^export\s+const\s+meta\s*=/.test(source.slice(offset))) {
    throw new SyntaxError('Workflow must begin with export const meta = metadata');
  }
  return source.slice(0, offset) + '      ' + source.slice(offset + 6);
}

export async function executeWorkflow(source, requestAgent, emit = () => {}, args = {}, context = {}) {
  const depth = context.depth ?? 0;
  const stagedDigests = context.stagedDigests ?? new Map();
  const binding = { depth, script_path: context.scriptPath,
    script_sha256: createHash('sha256').update(source).digest('hex') };
  if (depth >= MAX_WORKFLOW_DEPTH) throw new Error(`Workflow nesting exceeds ${MAX_WORKFLOW_DEPTH}`);
  let currentPhase = null;
  const active = new Set();
  const phase = (name) => {
    if (typeof name !== 'string' || !name.trim()) throw new TypeError('phase requires a non-empty name');
    currentPhase = name;
    emit({ event: 'phase', phase: name });
  };
  const agent = (prompt, options = {}) => {
    if (typeof prompt !== 'string') throw new TypeError('agent prompt must be a string');
    if (!options || typeof options !== 'object' || Array.isArray(options)) {
      throw new TypeError('agent options must be an object');
    }
    const spec = { ...options };
    if (spec.phase === undefined && currentPhase !== null) spec.phase = currentPhase;
    // JSON serialization fails here, before dispatch, for values the broker
    // cannot represent (cycles, BigInts). Schema and labels remain intact.
    const encoded = JSON.parse(JSON.stringify(spec));
    emit({ event: 'agent-start', label: encoded.label ?? null, phase: encoded.phase ?? null });
    const work = Promise.resolve().then(() => requestAgent(prompt, encoded, binding)).then(result => {
      emit({ event: 'agent-complete', label: encoded.label ?? null, phase: encoded.phase ?? null });
      return result;
    });
    active.add(work);
    // Handle the rejection without changing the promise returned to the script.
    work.then(() => active.delete(work), () => active.delete(work));
    return work;
  };
  const parallel = async (items) => {
    if (!Array.isArray(items)) throw new TypeError('parallel requires an array');
    return Promise.all(items.map(item => typeof item === 'function' ? item() : item));
  };
  const pipeline = async (items, ...stages) => {
    if (!Array.isArray(items) || stages.some(stage => typeof stage !== 'function')) {
      throw new TypeError('pipeline requires an array and stage functions');
    }
    return Promise.all(items.map(async (item, index) => {
      let value = item;
      for (const stage of stages) value = await stage(value, index);
      return value;
    }));
  };
  const log = (...values) => emit({ event: 'log', message: values.map(value =>
    typeof value === 'string' ? value : JSON.stringify(value)).join(' ') });
  const sleep = milliseconds => {
    if (typeof milliseconds !== 'number' || !Number.isFinite(milliseconds) || milliseconds < 0 || milliseconds > 2147483647) {
      throw new TypeError('sleep requires a finite non-negative millisecond duration');
    }
    return new Promise(resolve => setTimeout(resolve, milliseconds));
  };
  const engineOp = typeof context.engineOp === 'function' ? (op, params) => {
    if (typeof op !== 'string' || !op.trim()) throw new TypeError('engineOp requires a non-empty operation name');
    if (!params || typeof params !== 'object' || Array.isArray(params)) {
      throw new TypeError('engineOp params must be an object');
    }
    const encoded = JSON.parse(JSON.stringify(params));
    emit({ event: 'invoke-start', op });
    if (depth !== 0 && ['gate.host_run', 'gate.host_receipt', 'workflow.host_start'].includes(op)) {
      throw new Error('Nested workflows do not own host operations');
    }
    const work = Promise.resolve().then(() => context.engineOp(op, encoded, binding)).then(result => {
      if (op === 'gate.host_run' && result?.stage_reply?.twin_sha256) {
        const stage = result.stage_reply;
        stagedDigests.set(resolve(context.cwd ?? process.cwd(), stage.twin_path), stage.twin_sha256);
      }
      emit({ event: 'invoke-complete', op });
      return result;
    });
    active.add(work);
    work.then(() => active.delete(work), () => active.delete(work));
    return work;
  } : undefined;
  if (engineOp) Object.defineProperty(engineOp, 'directRun', { value: depth === 0 });
  if (engineOp && depth === 1 && stagedDigests.get(context.scriptPath) === binding.script_sha256) {
    Object.defineProperty(engineOp, 'stagedLateRows', { value: true });
  }
  if (engineOp) Object.defineProperty(engineOp, 'hostGateOwned', { value: depth === 0 && context.hostGateOwned === true });
  if (engineOp && depth === 0 && context.hostWorkflow === true && !context.hostGateOwned) {
    Object.defineProperty(engineOp, 'hostWorkflow', { value: async scriptPath => {
      const root = await context.engineOp('workflow.host_start', { script_path: scriptPath }, binding);
      if (createHash('sha256').update(root.source).digest('hex') !== root.script_sha256) {
        throw new Error('Host root digest mismatch');
      }
      return executeWorkflow(root.source, requestAgent, emit, root.args,
        { ...context, stagedDigests, scriptPath: root.script_path, depth: 0, hostWorkflow: false, hostGateOwned: true });
    } });
  }
  const workflow = async (options, positionalArgs) => {
    if (!options || typeof options.scriptPath !== 'string' || !options.scriptPath.trim()) {
      throw new TypeError('workflow requires a scriptPath');
    }
    if (options.args !== undefined && positionalArgs !== undefined &&
        !isDeepStrictEqual(options.args, positionalArgs)) {
      throw new TypeError('workflow options.args and positional args disagree');
    }
    const nestedArgs = options.args !== undefined ? options.args :
      positionalArgs !== undefined ? positionalArgs : {};
    if (!nestedArgs || typeof nestedArgs !== 'object' || Array.isArray(nestedArgs)) {
      throw new TypeError('workflow args must be an object');
    }
    // Emitted relative paths are repository-relative, like the native harness
    // and the broker's subprocess cwd. Do not change process cwd: siblings run
    // concurrently and must never move one another's relative-path base.
    const scriptPath = resolve(context.cwd ?? process.cwd(), options.scriptPath);
    emit({ event: 'workflow-start', script_path: scriptPath, depth: depth + 1 });
    const bytes = readFileSync(scriptPath);
    if (stagedDigests.has(scriptPath) && createHash('sha256').update(bytes).digest('hex') !== stagedDigests.get(scriptPath)) {
      throw new Error('Staged workflow differs from host-issued source');
    }
    const result = await executeWorkflow(bytes.toString('utf8'), requestAgent, emit, nestedArgs,
      { ...context, stagedDigests, scriptPath, depth: depth + 1 });
    emit({ event: 'workflow-complete', script_path: scriptPath, depth: depth + 1 });
    return result;
  };
  const outputConsole = Object.fromEntries(['log', 'info', 'warn', 'error', 'debug'].map(method => [
    method, (...values) => console.error(...values),
  ]));
  // `args` is a global in the native Workflow harness, so put it on the
  // prototype scope rather than a function parameter: bound scripts declare
  // their own `const args` and must not fail with a duplicate declaration.
  const body = workflowBody(source);
  const factory = new Function('agent', 'parallel', 'pipeline', 'phase', 'args', 'console', 'workflow', 'log', 'sleep', 'engineOp',
    `return async function workflowBodyExecutor() {\n${body}\n};`);
  const run = factory(agent, parallel, pipeline, phase, args, outputConsole, workflow, log, sleep, engineOp);
  const result = await run();
  // Do not report completion while a deliberately detached agent still runs.
  await Promise.all([...active]);
  return result;
}

export async function main(argv = process.argv.slice(2)) {
  const flag = argv.indexOf('--staged-sha');
  const stagedSha = flag < 0 ? null : argv[flag + 1];
  if (flag >= 0) argv = argv.filter((_, i) => i !== flag && i !== flag + 1);
  if (argv.length < 1 || argv.length > 2) throw new Error('usage: node runtime.mjs SCRIPT [ARGS_JSON_FILE] [--staged-sha SHA256]');
  const target = resolve(argv[0]);
  const stagedDigests = new Map();
  let source = readFileSync(argv[0], 'utf8');
  if (flag >= 0) {
    if (!/^[0-9a-f]{64}$/.test(stagedSha ?? '')) throw new Error('--staged-sha requires 64 lowercase hex');
    stagedDigests.set(target, stagedSha);
    source = `export const meta = {};\nreturn await workflow({ scriptPath: ${JSON.stringify(target)}, args });`;
  }
  const args = argv[1] ? JSON.parse(readFileSync(argv[1], 'utf8')) : {};
  if (!args || typeof args !== 'object' || Array.isArray(args)) throw new TypeError('Workflow args must be an object');
  const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });
  const pending = new Map();
  let nextId = 0;
  let brokerClosed = false;
  let protocolError = null;
  const write = message => process.stdout.write(JSON.stringify(message) + '\n');
  const rejectAll = error => {
    for (const request of pending.values()) request.reject(error);
    pending.clear();
  };
  lines.on('line', line => {
    try {
      const reply = JSON.parse(line);
      const request = pending.get(reply.id);
      if (!request) throw new Error(`Unknown agent response id ${JSON.stringify(reply.id)}`);
      pending.delete(reply.id);
      if (Object.hasOwn(reply, 'error')) {
        const failure = new Error(String(reply.error));
        if (reply.denial && typeof reply.denial === 'object') {
          failure.name = 'AgentPolicyDenied';
          failure.denial = reply.denial;
          failure.message = failure.message.replace(/^AgentPolicyDenied: /, '');
        }
        request.reject(failure);
      }
      else if (Object.hasOwn(reply, 'result')) request.resolve(reply.result);
      else request.reject(new Error('Agent response requires result or error'));
    } catch (error) { protocolError = error; rejectAll(error); }
  });
  lines.on('close', () => {
    brokerClosed = true;
    rejectAll(new Error('Agent broker closed stdin before responding'));
  });
  const request = (type, payload) => new Promise((resolve, reject) => {
    if (protocolError || brokerClosed) {
      reject(protocolError ?? new Error('Agent broker closed stdin before responding'));
      return;
    }
    const id = String(++nextId);
    pending.set(id, { resolve, reject });
    write({ type, id, ...payload });
  });
  const requestAgent = (prompt, options, binding) => request('agent', { prompt, options, binding });
  try {
    const result = await executeWorkflow(source, requestAgent, event => write({ type: 'event', ...event }), args,
      { scriptPath: target, cwd: process.cwd(), stagedDigests,
        hostWorkflow: true,
        engineOp: (op, params, binding) => request('invoke', { op, params, binding }) });
    if (protocolError) throw protocolError;
    write({ type: 'result', result: result === undefined ? null : result });
    return 0;
  } catch (error) {
    write({ type: 'error', error: `${error.name}: ${error.message}`, stack: error.stack,
      ...(error.denial ? { kind: 'policy_denial', denial: error.denial } : {}) });
    return 1;
  } finally { lines.close(); }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().then(code => { process.exitCode = code; }, error => {
    process.stdout.write(JSON.stringify({ type: 'error', error: `${error.name}: ${error.message}` }) + '\n');
    process.exitCode = 1;
  });
}
