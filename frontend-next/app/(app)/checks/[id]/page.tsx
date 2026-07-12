"use client";

import { useParams } from "next/navigation";

import CheckDetail from "@/components/CheckDetail";

export default function CheckDetailPage() {
  const { id } = useParams<{ id: string }>();
  // Key on the id so navigating between checks remounts with fresh stream state.
  return <CheckDetail key={id} id={id} />;
}
