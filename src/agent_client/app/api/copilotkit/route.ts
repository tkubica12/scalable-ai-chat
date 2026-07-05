import { NextRequest, NextResponse } from 'next/server';

export async function POST(_request: NextRequest) {
  return NextResponse.json(
    {
      error: 'CopilotKit runtime adapter is scaffolded; production traffic continues through /api/runs.'
    },
    { status: 501 }
  );
}
