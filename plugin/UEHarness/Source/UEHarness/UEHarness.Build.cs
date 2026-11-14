using UnrealBuildTool;

public class UEHarness : ModuleRules
{
	public UEHarness(ReadOnlyTargetRules Target) : base(Target)
	{
		PCHUsage = ModuleRules.PCHUsageMode.UseExplicitOrSharedPCHs;

		PublicIncludePaths.AddRange(new string[] { });

		PrivateIncludePaths.AddRange(new string[] { });

		PublicDependencyModuleNames.AddRange(new string[]
		{
			"Core",
			"CoreUObject",
			"Engine",
			"InputCore"
		});

		PrivateDependencyModuleNames.AddRange(new string[]
		{
			"UnrealEd",
			"EditorSubsystem",
			"Slate",
			"SlateCore",
			"Json",
			"JsonUtilities",
			"Sockets",
			"Networking",
			"AssetRegistry",
			"AssetTools",
			"BlueprintGraph",
			"Kismet",
			"KismetCompiler",
			"GraphEditor",
			"EditorScriptingUtilities",
			"LevelEditor",
			"MaterialEditor",
			"RenderCore",
			"Projects",
			"UMG",
			"UMGEditor"
		});

		// Python bridge is optional: if the PythonScriptPlugin is enabled we drive it,
		// otherwise the native command surface still works on its own.
		if (Target.bBuildEditor)
		{
			PrivateDependencyModuleNames.Add("PythonScriptPlugin");
		}
	}
}
