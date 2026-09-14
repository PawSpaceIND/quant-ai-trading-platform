import { NextResponse } from "next/server";
import { conversations } from "@/lib/copilot";
export async function GET(
  _request: Request,
  { params }: { params: Promise<{ id: string }> },
) {
  const { id } = await params;
  const row = conversations(id)[0];
  return row
    ? NextResponse.json(row)
    : NextResponse.json({ error: "Conversation not found" }, { status: 404 });
}
