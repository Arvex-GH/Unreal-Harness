#pragma once

#include "CoreMinimal.h"
#include "Modules/ModuleManager.h"

DECLARE_LOG_CATEGORY_EXTERN(LogUEHarness, Log, All);

class FUEHarnessServer;

/**
 * Editor module entry point.
 *
 * Boots a loopback TCP server inside the editor process. The harness (any
 * local process) speaks newline-delimited JSON to it: no HTTP overhead, no
 * MCP hop, one round trip per editor frame at worst.
 */
class FUEHarnessModule : public IModuleInterface
{
public:
	virtual void StartupModule() override;
	virtual void ShutdownModule() override;

	static FUEHarnessModule& Get();
	bool IsServerRunning() const;

private:
	TSharedPtr<FUEHarnessServer> Server;
};
