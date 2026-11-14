#include "UEHarnessModule.h"
#include "UEHarnessServer.h"
#include "UEHarnessCommands.h"
#include "Misc/CoreDelegates.h"
#include "Modules/ModuleManager.h"
#include "HAL/IConsoleManager.h"

DEFINE_LOG_CATEGORY(LogUEHarness);

IMPLEMENT_MODULE(FUEHarnessModule, UEHarness)

FUEHarnessModule& FUEHarnessModule::Get()
{
	return FModuleManager::LoadModuleChecked<FUEHarnessModule>("UEHarness");
}

void FUEHarnessModule::StartupModule()
{
	int32 Port = 34567;
	if (FParse::Param(FCommandLine::Get(), TEXT("harnessport")))
	{
		FParse::Value(FCommandLine::Get(), TEXT("harnessport="), Port);
	}
	Port = FPlatformMisc::GetEnvironmentVariable(TEXT("UE_HARNESS_PORT")).IsEmpty()
		? Port
		: FCString::Atoi(*FPlatformMisc::GetEnvironmentVariable(TEXT("UE_HARNESS_PORT")));

	Server = MakeShared<FUEHarnessServer>();
	if (Server->Start(Port))
	{
		UE_LOG(LogUEHarness, Log, TEXT("Bridge listening on 127.0.0.1:%d"), Port);
	}
	else
	{
		UE_LOG(LogUEHarness, Warning, TEXT("Could not bind bridge port %d (already in use?)"), Port);
	}

	// Capture editor log output so the agent can read compile errors itself.
	UEHarness::SetLogCapture(true);
}

void FUEHarnessModule::ShutdownModule()
{
	UEHarness::SetLogCapture(false);
	if (Server.IsValid())
	{
		Server->Stop();
		Server.Reset();
	}
}

bool FUEHarnessModule::IsServerRunning() const
{
	return Server.IsValid() && Server->IsRunning();
}
