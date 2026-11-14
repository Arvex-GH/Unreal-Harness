#pragma once

#include "CoreMinimal.h"
#include "Dom/JsonObject.h"

/**
 * Command surface reachable from the harness.
 *
 * Every handler runs on the game thread (the server marshals) and is wrapped in
 * a scoped editor transaction whose name shows up in the Undo history, so an AI
 * edit is a normal, undoable editor action.
 */
namespace UEHarness
{
	using FJson = TSharedPtr<FJsonObject>;

	/** Returns true if the command was understood. */
	bool Dispatch(const FString& Command, const FJson& Args, FJson& OutResult, FString& OutError);

	/** Static description of the engine/plugin for capability negotiation. */
	FJson Handshake();

	/** Append a line to the shared log ring buffer (used by the log hook). */
	void PushLogLine(const FString& Line);
	void SetLogCapture(bool bEnabled);
	TArray<FString> DrainLogLines(int32 MaxLines);
}
