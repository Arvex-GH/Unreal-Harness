#include "UEHarnessServer.h"
#include "UEHarnessModule.h"
#include "UEHarnessCommands.h"
#include "UEHarnessJson.h"

#include "Async/Async.h"
#include "Common/TcpSocketBuilder.h"
#include "HAL/PlatformProcess.h"
#include "HAL/RunnableThread.h"
#include "Interfaces/IPv4/IPv4Address.h"
#include "Interfaces/IPv4/IPv4Endpoint.h"
#include "Misc/CoreMiscGlobals.h"
#include "Misc/EngineVersionComparison.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonSerializer.h"
#include "SocketSubsystem.h"
#include "Sockets.h"
#include "Dom/JsonObject.h"

DEFINE_LOG_CATEGORY_STATIC(LogHarnessSrv, Log, All);

// ---------------------------------------------------------------------------
// Worker: accepts clients and serves each on its own thread.
// ---------------------------------------------------------------------------
class FUEHarnessWorker : public FRunnable
{
public:
	FUEHarnessWorker(FSocket* InListener, FUEHarnessServer* InOwner)
		: Listener(InListener), Owner(InOwner) {}

	virtual ~FUEHarnessWorker() override { Stop(); }

	virtual uint32 Run() override
	{
		while (!bStop)
		{
			bool bPending = false;
			if (!Listener->HasPendingConnection(bPending) || !bPending)
			{
				FPlatformProcess::Sleep(0.01f);
				continue;
			}

			FSocket* Client = Listener->Accept(TEXT("HarnessClient"));
			if (!Client)
			{
				continue;
			}
			Client->SetNonBlocking(false);
			Client->SetNoDelay(true);
			ServeClient(Client);
		}
		return 0;
	}

	virtual void Stop() override { bStop = true; }

private:
	void ServeClient(FSocket* Client)
	{
		TArray<uint8> Buffer;
		Buffer.Reserve(64 * 1024);
		uint8 Chunk[16384];

		while (!bStop && Client->GetConnectionState() == SCS_Connected)
		{
			int32 Read = 0;
			if (!Client->Recv(Chunk, UE_ARRAY_COUNT(Chunk), Read))
			{
				// Non-fatal for non-blocking/partial reads; treat hard failure as close.
				if (Client->GetConnectionState() != SCS_Connected)
				{
					break;
				}
				FPlatformProcess::Sleep(0.002f);
				continue;
			}
			if (Read <= 0)
			{
				break; // peer closed
			}
			Buffer.Append(Chunk, Read);

			int32 NewlineIdx = INDEX_NONE;
			while (Buffer.Find((uint8)'\n', NewlineIdx))
			{
				const int32 LineLen = NewlineIdx;
				FUTF8ToTCHAR Converted(reinterpret_cast<const ANSICHAR*>(Buffer.GetData()), LineLen);
				FString Line(Converted.Length(), Converted.Get());
#if UE_VERSION_OLDER_THAN(5, 5, 0)
				Buffer.RemoveAt(0, LineLen + 1, false);
#else
				Buffer.RemoveAt(0, LineLen + 1, EAllowShrinking::No);
#endif

				Line.TrimStartAndEndInline();
				if (Line.IsEmpty())
				{
					continue;
				}
				const FString Reply = Owner->HandleClientLine(Line);
				SendLine(Client, Reply);
			}

			// Protect against a client that never sends a newline.
			if (Buffer.Num() > 32 * 1024 * 1024)
			{
				Buffer.Empty();
			}
		}

		Client->Close();
		ISocketSubsystem::Get(PLATFORM_SOCKETSUBSYSTEM)->DestroySocket(Client);
	}

	static void SendLine(FSocket* Client, const FString& Line)
	{
		FTCHARToUTF8 Converted(*Line);
		TArray<uint8> Payload(reinterpret_cast<const uint8*>(Converted.Get()), Converted.Length());
		Payload.Add('\n');

		int32 Offset = 0;
		while (Offset < Payload.Num())
		{
			int32 Sent = 0;
			if (!Client->Send(Payload.GetData() + Offset, Payload.Num() - Offset, Sent) || Sent <= 0)
			{
				break;
			}
			Offset += Sent;
		}
	}

	FSocket* Listener = nullptr;
	FUEHarnessServer* Owner = nullptr;
	FThreadSafeBool bStop;
};

// ---------------------------------------------------------------------------
// Server
// ---------------------------------------------------------------------------
FUEHarnessServer::FUEHarnessServer() {}

FUEHarnessServer::~FUEHarnessServer()
{
	Stop();
}

bool FUEHarnessServer::Start(int32 Port)
{
	ISocketSubsystem* Sockets = ISocketSubsystem::Get(PLATFORM_SOCKETSUBSYSTEM);
	if (!Sockets)
	{
		return false;
	}

	FIPv4Address Address;
	FIPv4Address::Parse(TEXT("127.0.0.1"), Address);
	const FIPv4Endpoint Endpoint(Address, static_cast<uint16>(Port));

	Listener = FTcpSocketBuilder(TEXT("UEHarnessListener"))
		.AsReusable()
		.BoundToEndpoint(Endpoint)
		.Listening(8);
	if (!Listener)
	{
		UE_LOG(LogHarnessSrv, Warning, TEXT("Failed to bind port %d"), Port);
		return false;
	}

	BoundPort = Port;
	bRunning = true;
	Worker = MakeShared<FUEHarnessWorker>(Listener, this);
	Thread = FRunnableThread::Create(Worker.Get(), TEXT("UEHarnessWorker"));
	if (!Thread)
	{
		Worker.Reset();
		Listener->Close();
		Sockets->DestroySocket(Listener);
		Listener = nullptr;
		bRunning = false;
		return false;
	}
	return true;
}

void FUEHarnessServer::Stop()
{
	if (!bRunning)
	{
		return;
	}
	bRunning = false;

	if (Worker.IsValid())
	{
		Worker->Stop();
	}
	if (Listener)
	{
		Listener->Close();
	}
	if (Thread)
	{
		Thread->WaitForCompletion();
		delete Thread;
		Thread = nullptr;
	}
	Worker.Reset();
	if (Listener)
	{
		ISocketSubsystem::Get(PLATFORM_SOCKETSUBSYSTEM)->DestroySocket(Listener);
		Listener = nullptr;
	}
}

FString FUEHarnessServer::HandleClientLine(const FString& Line)
{
	TSharedPtr<FJsonObject> Request;
	const TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(Line);
	if (!FJsonSerializer::Deserialize(Reader, Request) || !Request.IsValid())
	{
		return TEXT("{\"id\":0,\"ok\":false,\"error\":\"malformed json\"}");
	}

	int64 Id = 0;
	Request->TryGetNumberField(TEXT("id"), Id);

	FString Command;
	Request->TryGetStringField(TEXT("cmd"), Command);

	int64 Flags = 0;
	Request->TryGetNumberField(TEXT("flags"), Flags);
	const bool bNoTransaction = (Flags & 1) != 0;

	TSharedPtr<FJsonObject> Args = MakeShared<FJsonObject>();
	const TSharedPtr<FJsonObject>* ArgsPtr = nullptr;
	if (Request->TryGetObjectField(TEXT("args"), ArgsPtr) && ArgsPtr)
	{
		Args = *ArgsPtr;
	}

	bool bOk = false;
	FString Error;
	TSharedPtr<FJsonObject> Result = MakeShared<FJsonObject>();

	if (IsEngineExitRequested())
	{
		return TEXT("{\"id\":0,\"ok\":false,\"error\":\"engine shutting down\"}");
	}

	// Execute on the game thread: every editor API we touch requires it.
	FEvent* Done = FPlatformProcess::GetSynchEventFromPool();
	AsyncTask(ENamedThreads::GameThread, [&]()
	{
		if (bNoTransaction)
		{
			bOk = UEHarness::Dispatch(Command, Args, Result, Error);
		}
		else
		{
			const FScopedTransaction Transaction(
				FText::FromString(FString::Printf(TEXT("Harness: %s"), *Command)));
			bOk = UEHarness::Dispatch(Command, Args, Result, Error);
		}
		Done->Trigger();
	});
	Done->Wait();
	FPlatformProcess::ReturnSynchEventToPool(Done);

	if (!bOk)
	{
		UE_LOG(LogHarnessSrv, Verbose, TEXT("%s failed: %s"), *Command, *Error);
	}
	return UEHarnessJson::MakeReply(Id, bOk, Result, Error);
}
