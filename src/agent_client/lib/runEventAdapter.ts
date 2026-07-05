type RepoRunEvent = {
  type: string;
  runId: string;
  threadId: string;
  sequence: number;
  timestamp: string;
  [key: string]: unknown;
};

const eventTypeMap: Record<string, string> = {
  RunStarted: 'RUN_STARTED',
  RunFinished: 'RUN_FINISHED',
  RunError: 'RUN_ERROR',
  RunCancelled: 'RUN_CANCELLED',
  TextMessageStart: 'TEXT_MESSAGE_START',
  TextMessageContent: 'TEXT_MESSAGE_CONTENT',
  TextMessageEnd: 'TEXT_MESSAGE_END',
  ToolCallStart: 'TOOL_CALL_START',
  ToolCallArgs: 'TOOL_CALL_ARGS',
  ToolCallEnd: 'TOOL_CALL_END',
  ToolCallResult: 'TOOL_CALL_RESULT',
  StateSnapshot: 'STATE_SNAPSHOT',
  StateDelta: 'STATE_DELTA'
};

export function mapRepoEventToAgUi(event: RepoRunEvent) {
  return {
    ...event,
    type: eventTypeMap[event.type] ?? 'CUSTOM',
    rawType: event.type
  };
}

export async function createRun(frontServiceUrl: string, input: unknown) {
  const response = await fetch(`${frontServiceUrl}/api/runs`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input)
  });
  if (!response.ok) {
    throw new Error(`Run creation failed with status ${response.status}`);
  }
  return response.json() as Promise<{ runId: string; eventsUrl: string; threadId: string }>;
}
