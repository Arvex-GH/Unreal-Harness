#pragma once

#include "CoreMinimal.h"
#include "HAL/Runnable.h"
#include "HAL/ThreadSafeBool.h"

class FSocket;
class FRunnableThread;
class FUEHarnessWorker;

/**
 * Loopback TCP JSON-lines server living inside the editor process.
 *
 *  - accept thread + one worker thread per client
 *  - every command is marshalled to the game thread and wrapped in a transaction
 *  - newline-delimited JSON in, newline-delimited JSON out
 *  - pipelining is natural: write N requests, read N replies
 *
 * See docs/PROTOCOL.md for the wire format.
 */
class FUEHarnessServer
{
public:
	FUEHarnessServer();
	~FUEHarnessServer();

	bool Start(int32 Port);
	void Stop();
	bool IsRunning() const { return bRunning; }

	/** Called on a worker thread; executes the command on the game thread. */
	FString HandleClientLine(const FString& Line);

private:
	FSocket* Listener = nullptr;
	TSharedPtr<FUEHarnessWorker> Worker;
	FRunnableThread* Thread = nullptr;
	FThreadSafeBool bRunning;
	int32 BoundPort = 0;
};
