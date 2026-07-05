'use client';

import { CopilotKit } from '@copilotkit/react-core';
import { CopilotChat } from '@copilotkit/react-ui';
import { mapRepoEventToAgUi } from '../lib/runEventAdapter';

const frontServiceUrl = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000';

export default function Page() {
  const mappedPreview = mapRepoEventToAgUi({
    type: 'TextMessageContent',
    runId: 'run_preview',
    threadId: 'thread_preview',
    sequence: 1,
    timestamp: new Date().toISOString(),
    messageId: 'msg_preview',
    delta: 'Preview'
  });

  return (
    <CopilotKit runtimeUrl="/api/copilotkit">
      <main className="shell">
        <section className="panel">
          <h1>CopilotKit migration client</h1>
          <p>
            This parallel client keeps the current Svelte app intact while proving the
            CopilotKit surface and repo event adapter can coexist with the durable run API.
          </p>
          <dl>
            <dt>Front service</dt>
            <dd>{frontServiceUrl}</dd>
            <dt>Adapter preview</dt>
            <dd>{mappedPreview.type}</dd>
          </dl>
        </section>
        <section className="chat">
          <CopilotChat
            labels={{
              title: 'Agent runtime demo',
              initial: 'The CopilotKit UI shell is ready. Runtime wiring is intentionally adapter-based.'
            }}
          />
        </section>
      </main>
    </CopilotKit>
  );
}
