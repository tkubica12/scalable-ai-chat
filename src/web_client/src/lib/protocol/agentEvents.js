const AGENT_EVENT_TYPES = new Set([
  'RunStarted',
  'RunFinished',
  'RunError',
  'RunCancelled',
  'TextMessageStart',
  'TextMessageContent',
  'TextMessageEnd',
  'ToolCallStart',
  'ToolCallArgs',
  'ToolCallEnd',
  'ToolCallResult',
  'StateSnapshot',
  'StateDelta',
  'ArtifactCreated',
  'ArtifactDelta',
  'ArtifactFinalized',
  'Heartbeat',
  'Usage',
  'SafetyVerdict'
]);

export function parseAgentEvent(data) {
  const event = typeof data === 'string' ? JSON.parse(data) : data;
  if (!event || typeof event !== 'object') {
    throw new Error('Agent event must be an object');
  }
  if (!AGENT_EVENT_TYPES.has(event.type)) {
    throw new Error(`Unsupported agent event type: ${event.type}`);
  }
  if (!event.runId || !event.threadId || !Number.isInteger(event.sequence) || !event.timestamp) {
    throw new Error('Agent event is missing runId, threadId, sequence, or timestamp');
  }
  return event;
}

export function applyAgentEventToMessage(event, currentContent) {
  if (event.type === 'TextMessageContent') {
    return `${currentContent}${event.delta ?? ''}`;
  }
  if (event.type === 'RunError') {
    return event.error?.message || 'Run failed';
  }
  return currentContent;
}
