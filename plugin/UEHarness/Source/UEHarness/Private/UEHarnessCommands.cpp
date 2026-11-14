// Unreal Harness - native command surface.
//
// Targets UE 5.1 - 5.8+. Where APIs moved between versions the guarded block is
// marked with // [VER]. Everything here runs on the game thread inside a
// transaction, so AI edits behave exactly like human edits in the Undo stack.

#include "UEHarnessCommands.h"
#include "UEHarnessModule.h"

#include "Editor.h"
#include "WidgetBlueprint.h"
#include "Blueprint/UserWidget.h"
#include "Blueprint/WidgetBlueprintGeneratedClass.h"
#include "Blueprint/WidgetTree.h"
#include "Components/CanvasPanel.h"
#include "Components/CanvasPanelSlot.h"
#include "Components/PanelWidget.h"
#include "Components/TextBlock.h"
#include "Widgets/Layout/Anchors.h"
#include "UObject/Package.h"
#include "Misc/PackageName.h"
#include "Engine/Blueprint.h"
#include "Engine/BlueprintGeneratedClass.h"
#include "Engine/Level.h"
#include "Engine/World.h"
#include "Engine/Selection.h"
#include "GameFramework/Actor.h"
#include "Kismet2/BlueprintEditorUtils.h"
#include "Kismet2/KismetEditorUtilities.h"
#include "Kismet2/CompilerResultsLog.h"
#include "EdGraph/EdGraph.h"
#include "EdGraph/EdGraphNode.h"
#include "EdGraph/EdGraphPin.h"
#include "EdGraphSchema_K2.h"
#include "K2Node_CallFunction.h"
#include "K2Node_Event.h"
#include "K2Node_CustomEvent.h"
#include "K2Node_VariableGet.h"
#include "K2Node_VariableSet.h"
#include "K2Node_IfThenElse.h"
#include "K2Node_ExecutionSequence.h"
#include "K2Node_MacroInstance.h"
#include "K2Node_CommutativeAssociativeBinaryOperator.h"
#include "K2Node_DynamicCast.h"
#include "K2Node_Timeline.h"
#include "K2Node_ComponentBoundEvent.h"
#include "K2Node_FunctionEntry.h"
#include "K2Node_FunctionResult.h"
#include "Kismet/KismetSystemLibrary.h"
#include "AssetRegistry/AssetRegistryModule.h"
#include "AssetRegistry/ARFilter.h"
#include "AssetToolsModule.h"
#include "IAssetTools.h"
#include "EditorAssetLibrary.h"
#include "Editor/EditorEngine.h"
#include "Factories/BlueprintFactory.h"
#include "Materials/Material.h"
#include "Materials/MaterialInstanceConstant.h"
#include "Materials/MaterialExpression.h"
#include "Factories/MaterialFactoryNew.h"
#include "Misc/FileHelper.h"
#include "Misc/Paths.h"
#include "Misc/OutputDeviceRedirector.h"
#include "HAL/IConsoleManager.h"
#include "ScopedTransaction.h"
#include "Subsystems/EditorActorSubsystem.h"
#include "Framework/Application/SlateApplication.h"
#include "EngineUtils.h"
#include "Misc/EngineVersionComparison.h"
#include "Runtime/Launch/Resources/Version.h"
#include "IPythonScriptPlugin.h"
#include "PythonScriptTypes.h"

DEFINE_LOG_CATEGORY_STATIC(LogHarnessCmd, Log, All);

namespace
{
	// ---------------------------------------------------------------- helpers
	FORCEINLINE bool GetStr(const UEHarness::FJson& Args, const TCHAR* Key, FString& Out, bool bRequired = true)
	{
		if (Args.IsValid() && Args->TryGetStringField(Key, Out))
		{
			return true;
		}
		if (bRequired)
		{
			Out = FString::Printf(TEXT("missing argument '%s'"), Key);
		}
		return false;
	}

	FORCEINLINE int32 GetInt(const UEHarness::FJson& Args, const TCHAR* Key, int32 Default = 0)
	{
		int32 V = Default;
		if (Args.IsValid()) { Args->TryGetNumberField(Key, V); }
		return V;
	}

	FORCEINLINE double GetNum(const UEHarness::FJson& Args, const TCHAR* Key, double Default = 0.0)
	{
		double V = Default;
		if (Args.IsValid()) { Args->TryGetNumberField(Key, V); }
		return V;
	}

	FORCEINLINE bool GetBool(const UEHarness::FJson& Args, const TCHAR* Key, bool Default = false)
	{
		bool V = Default;
		if (Args.IsValid()) { Args->TryGetBoolField(Key, V); }
		return V;
	}

	UObject* LoadAny(const FString& Path)
	{
		FString P = Path;
		if (!P.Contains(TEXT(".")))
		{
			FString Name = FPaths::GetBaseFilename(P);
			P = FString::Printf(TEXT("%s.%s"), *P, *Name);
		}
		return LoadObject<UObject>(nullptr, *P);
	}

	UBlueprint* LoadBlueprint(const FString& Path, FString& OutError)
	{
		UBlueprint* BP = Cast<UBlueprint>(LoadAny(Path));
		if (!BP)
		{
			OutError = FString::Printf(TEXT("blueprint not found: %s"), *Path);
		}
		return BP;
	}

	/** Find a UFunction on a class chain by name (case-insensitive, 'K2_' aware). */
	UFunction* FindFunction(UClass* Class, const FString& Name)
	{
		if (!Class) { return nullptr; }
		UFunction* Fn = Class->FindFunctionByName(FName(*Name));
		if (!Fn)
		{
			FString K2Name = Name.StartsWith(TEXT("K2_")) ? Name : (TEXT("K2_") + Name);
			Fn = Class->FindFunctionByName(FName(*K2Name));
		}
		return Fn;
	}

	TSharedPtr<FJsonObject> PinToJson(const UEdGraphPin* Pin)
	{
		auto J = MakeShared<FJsonObject>();
		J->SetStringField(TEXT("name"), Pin->PinName.ToString());
		J->SetStringField(TEXT("type"), Pin->PinType.PinCategory.ToString());
		J->SetStringField(TEXT("sub_type"), Pin->PinType.PinSubCategoryObject.IsValid()
			? Pin->PinType.PinSubCategoryObject->GetName() : TEXT(""));
		J->SetStringField(TEXT("direction"), Pin->Direction == EGPD_Input ? TEXT("in") : TEXT("out"));
		J->SetBoolField(TEXT("connected"), Pin->LinkedTo.Num() > 0);
		J->SetBoolField(TEXT("is_exec"), Pin->PinType.PinCategory == UEdGraphSchema_K2::PC_Exec);
		J->SetStringField(TEXT("default"), Pin->GetDefaultAsString());
		return J;
	}

	TSharedPtr<FJsonObject> NodeToJson(UEdGraphNode* Node)
	{
		auto J = MakeShared<FJsonObject>();
		J->SetStringField(TEXT("id"), Node->NodeGuid.ToString());
		J->SetStringField(TEXT("class"), Node->GetClass()->GetName());
		J->SetStringField(TEXT("title"), Node->GetNodeTitle(ENodeTitleType::ListView).ToString());
		J->SetNumberField(TEXT("x"), Node->NodePosX);
		J->SetNumberField(TEXT("y"), Node->NodePosY);

		TArray<TSharedPtr<FJsonValue>> Inputs, Outputs;
		for (UEdGraphPin* Pin : Node->Pins)
		{
			if (!Pin || Pin->bHidden) { continue; }
			(Pin->Direction == EGPD_Input ? Inputs : Outputs).Add(MakeShared<FJsonValueObject>(PinToJson(Pin)));
		}
		J->SetArrayField(TEXT("inputs"), Inputs);
		J->SetArrayField(TEXT("outputs"), Outputs);
		return J;
	}

	UEdGraph* FindGraph(UBlueprint* BP, const FString& GraphName)
	{
		if (GraphName.IsEmpty() || GraphName.Equals(TEXT("EventGraph"), ESearchCase::IgnoreCase))
		{
			return FBlueprintEditorUtils::FindEventGraph(BP);
		}
		for (UEdGraph* G : BP->FunctionGraphs)
		{
			if (G && G->GetName().Equals(GraphName, ESearchCase::IgnoreCase)) { return G; }
		}
		for (UEdGraph* G : BP->UbergraphPages)
		{
			if (G && G->GetName().Equals(GraphName, ESearchCase::IgnoreCase)) { return G; }
		}
		// create on demand when asked for a function graph
		if (UEdGraph* NewGraph = FBlueprintEditorUtils::CreateNewGraph(
			BP, FName(*GraphName), UEdGraph::StaticClass(), UEdGraphSchema_K2::StaticClass()))
		{
#if ENGINE_MAJOR_VERSION >= 5 && ENGINE_MINOR_VERSION >= 5
			FBlueprintEditorUtils::AddFunctionGraph(BP, NewGraph, /*bIsUserCreated*/ true, nullptr);
#else
			FBlueprintEditorUtils::AddFunctionGraph<UClass>(BP, NewGraph, /*bIsUserCreated*/ true, nullptr);
#endif
			return NewGraph;
		}
		return nullptr;
	}

	UEdGraphNode* FindNode(UEdGraph* Graph, const FString& NodeId)
	{
		FGuid Guid;
		if (!FGuid::Parse(NodeId, Guid)) { return nullptr; }
		for (UEdGraphNode* N : Graph->Nodes)
		{
			if (N && N->NodeGuid == Guid) { return N; }
		}
		return nullptr;
	}

	UEdGraphPin* FindPin(UEdGraphNode* Node, const FString& PinName, EEdGraphPinDirection Dir)
	{
		for (UEdGraphPin* P : Node->Pins)
		{
			if (P && P->Direction == Dir && P->PinName.ToString().Equals(PinName, ESearchCase::IgnoreCase))
			{
				return P;
			}
		}
		// fall back: first exec pin when the caller said "then"/"exec"/""
		if (PinName.IsEmpty() || PinName.Equals(TEXT("then"), ESearchCase::IgnoreCase)
			|| PinName.Equals(TEXT("exec"), ESearchCase::IgnoreCase))
		{
			for (UEdGraphPin* P : Node->Pins)
			{
				if (P && P->Direction == Dir && P->PinType.PinCategory == UEdGraphSchema_K2::PC_Exec) { return P; }
			}
		}
		return nullptr;
	}

	UEdGraphPin* SplitPinSpec(UEdGraph* Graph, const FString& Spec, EEdGraphPinDirection Dir, FString& OutError)
	{
		FString NodeId, PinName;
		if (!Spec.Split(TEXT(":"), &NodeId, &PinName)) { NodeId = Spec; }
		UEdGraphNode* Node = FindNode(Graph, NodeId);
		if (!Node) { OutError = FString::Printf(TEXT("node not found: %s"), *NodeId); return nullptr; }
		UEdGraphPin* Pin = FindPin(Node, PinName, Dir);
		if (!Pin) { OutError = FString::Printf(TEXT("pin '%s' not found on %s"), *PinName, *Node->GetNodeTitle(ENodeTitleType::ListView).ToString()); }
		return Pin;
	}

	// ------------------------------------------------------------- log capture
	static FCriticalSection LogLock;
	static TArray<FString> LogRing;
	static bool bCapturing = false;
	static const int32 LogRingMax = 4000;

	class FHarnessLogHook : public FOutputDevice
	{
	public:
		virtual void Serialize(const TCHAR* Msg, ELogVerbosity::Type Verbosity, const FName& Category) override
		{
			if (!bCapturing) { return; }
			FScopeLock Lock(&LogLock);
			LogRing.Add(FString::Printf(TEXT("[%s] %s"), *Category.ToString(), Msg));
#if UE_VERSION_OLDER_THAN(5, 5, 0)
			if (LogRing.Num() > LogRingMax) { LogRing.RemoveAt(0, LogRing.Num() - LogRingMax, false); }
#else
			if (LogRing.Num() > LogRingMax) { LogRing.RemoveAt(0, LogRing.Num() - LogRingMax, EAllowShrinking::No); }
#endif
		}
	};
	static FHarnessLogHook* LogHook = nullptr;
}

// ---------------------------------------------------------------- public API
namespace UEHarness
{
	void PushLogLine(const FString& Line)
	{
		FScopeLock Lock(&LogLock);
		LogRing.Add(Line);
	}

	void SetLogCapture(bool bEnabled)
	{
		bCapturing = bEnabled;
		if (bEnabled && !LogHook)
		{
			LogHook = new FHarnessLogHook();
			GLog->AddOutputDevice(LogHook);
		}
		else if (!bEnabled && LogHook)
		{
			GLog->RemoveOutputDevice(LogHook);
			delete LogHook;
			LogHook = nullptr;
		}
	}

	TArray<FString> DrainLogLines(int32 MaxLines)
	{
		FScopeLock Lock(&LogLock);
		TArray<FString> Out;
		const int32 Start = FMath::Max(0, LogRing.Num() - MaxLines);
		Out.Append(LogRing.GetData() + Start, LogRing.Num() - Start);
		return Out;
	}

	FJson Handshake()
	{
		auto J = MakeShared<FJsonObject>();
		J->SetStringField(TEXT("plugin"), TEXT("UEHarness"));
		J->SetNumberField(TEXT("protocol"), 2);
		J->SetStringField(TEXT("engine_version"), FEngineVersion::Current().ToString(EVersionComponent::Patch));
		J->SetStringField(TEXT("project"), FApp::GetProjectName());
		J->SetBoolField(TEXT("pie_running"), GEditor && GEditor->PlayWorld != nullptr);

		TArray<TSharedPtr<FJsonValue>> Verbs = {
			MakeShared<FJsonValueString>(TEXT("editor.console")),
			MakeShared<FJsonValueString>(TEXT("editor.info")),
			MakeShared<FJsonValueString>(TEXT("editor.log")),
			MakeShared<FJsonValueString>(TEXT("editor.save_all")),
			MakeShared<FJsonValueString>(TEXT("editor.undo")),
			MakeShared<FJsonValueString>(TEXT("editor.select")),
			MakeShared<FJsonValueString>(TEXT("asset.search")),
			MakeShared<FJsonValueString>(TEXT("asset.create")),
			MakeShared<FJsonValueString>(TEXT("asset.rename")),
			MakeShared<FJsonValueString>(TEXT("asset.delete")),
			MakeShared<FJsonValueString>(TEXT("bp.create")),
			MakeShared<FJsonValueString>(TEXT("bp.variable_add")),
			MakeShared<FJsonValueString>(TEXT("bp.graph_read")),
			MakeShared<FJsonValueString>(TEXT("bp.add_node")),
			MakeShared<FJsonValueString>(TEXT("bp.connect")),
			MakeShared<FJsonValueString>(TEXT("bp.disconnect")),
			MakeShared<FJsonValueString>(TEXT("bp.set_pin_default")),
			MakeShared<FJsonValueString>(TEXT("bp.build_graph")),
			MakeShared<FJsonValueString>(TEXT("bp.compile")),
			MakeShared<FJsonValueString>(TEXT("bp.list_graphs")),
			MakeShared<FJsonValueString>(TEXT("umg.create_widget")),
			MakeShared<FJsonValueString>(TEXT("actor.spawn")),
			MakeShared<FJsonValueString>(TEXT("actor.find")),
			MakeShared<FJsonValueString>(TEXT("actor.set_property")),
			MakeShared<FJsonValueString>(TEXT("actor.call")),
			MakeShared<FJsonValueString>(TEXT("pie.start")),
			MakeShared<FJsonValueString>(TEXT("pie.stop")),
			MakeShared<FJsonValueString>(TEXT("python.exec")),
		};
		J->SetArrayField(TEXT("verbs"), Verbs);
		return J;
	}

	bool Dispatch(const FString& Cmd, const FJson& Args, FJson& Out, FString& Err)
	{
		if (!Out.IsValid()) { Out = MakeShared<FJsonObject>(); }

		// ------------------------------------------------------------ meta
		if (Cmd == TEXT("handshake") || Cmd == TEXT("ping"))
		{
			Out = Handshake();
			return true;
		}

		// ---------------------------------------------------------- editor
		if (Cmd == TEXT("editor.console"))
		{
			FString CmdLine;
			if (!GetStr(Args, TEXT("command"), CmdLine)) { Err = CmdLine; return false; }
			FStringOutputDevice Output;
			GEditor->Exec(GEditor->GetEditorWorldContext().World(), *CmdLine, Output);
			Out->SetStringField(TEXT("output"), Output);
			return true;
		}
		if (Cmd == TEXT("editor.log"))
		{
			const int32 Max = GetInt(Args, TEXT("lines"), 200);
			FString Filter;
			Args->TryGetStringField(TEXT("filter"), Filter);
			TArray<TSharedPtr<FJsonValue>> Lines;
			for (const FString& L : DrainLogLines(Max))
			{
				if (Filter.IsEmpty() || L.Contains(Filter, ESearchCase::IgnoreCase))
				{
					Lines.Add(MakeShared<FJsonValueString>(L));
				}
			}
			Out->SetArrayField(TEXT("lines"), Lines);
			return true;
		}
		if (Cmd == TEXT("editor.save_all"))
		{
			const bool bOk = UEditorAssetLibrary::SaveDirectory(TEXT("/Game"), true, true);
			Out->SetBoolField(TEXT("saved"), bOk);
			return true;
		}
		if (Cmd == TEXT("editor.undo"))
		{
			GEditor->UndoTransaction();
			return true;
		}
		if (Cmd == TEXT("editor.select"))
		{
			FString Path;
			GetStr(Args, TEXT("asset"), Path, false);
			if (UObject* Obj = LoadAny(Path))
			{
				GEditor->GetSelectedObjects()->DeselectAll();
				GEditor->GetSelectedObjects()->Select(Obj);
			}
			return true;
		}
		if (Cmd == TEXT("editor.info"))
		{
			Out->SetStringField(TEXT("project"), FApp::GetProjectName());
			Out->SetStringField(TEXT("project_dir"), FPaths::ProjectDir());
			Out->SetStringField(TEXT("engine_version"), FEngineVersion::Current().ToString());
			Out->SetStringField(TEXT("platform"), FPlatformProperties::PlatformName());
			int32 ActorCount = 0;
			if (UWorld* World = GEditor ? GEditor->GetEditorWorldContext().World() : nullptr)
			{
				for (TActorIterator<AActor> It(World); It; ++It) { ++ActorCount; }
			}
			Out->SetNumberField(TEXT("actor_count"), ActorCount);
			return true;
		}

		// ----------------------------------------------------------- assets
		if (Cmd == TEXT("asset.search"))
		{
			FString Query, ClassName, RootPath = TEXT("/Game");
			Args->TryGetStringField(TEXT("query"), Query);
			Args->TryGetStringField(TEXT("class"), ClassName);
			Args->TryGetStringField(TEXT("path"), RootPath);
			const int32 Limit = GetInt(Args, TEXT("limit"), 100);

			FARFilter Filter;
			Filter.PackagePaths.Add(FName(*RootPath));
			Filter.bRecursivePaths = true;
			Filter.bRecursiveClasses = true;
			if (!ClassName.IsEmpty())
			{
				if (UClass* FilterClass = UClass::TryFindTypeSlow<UClass>(*ClassName))
				{
					Filter.ClassPaths.Add(FTopLevelAssetPath(FilterClass->GetPackageName(), FilterClass->GetFName()));
				}
			}

			TArray<FAssetData> Assets;
			FAssetRegistryModule& AR = FModuleManager::LoadModuleChecked<FAssetRegistryModule>(TEXT("AssetRegistry"));
			AR.Get().GetAssets(Filter, Assets);
			TArray<TSharedPtr<FJsonValue>> OutArr;
			for (const FAssetData& A : Assets)
			{
				if (OutArr.Num() >= Limit) { break; }
				if (!Query.IsEmpty() && !A.AssetName.ToString().Contains(Query, ESearchCase::IgnoreCase)) { continue; }
				auto O = MakeShared<FJsonObject>();
				O->SetStringField(TEXT("name"), A.AssetName.ToString());
				O->SetStringField(TEXT("path"), A.GetObjectPathString());
				O->SetStringField(TEXT("class"), A.AssetClassPath.GetAssetName().ToString());
				OutArr.Add(MakeShared<FJsonValueObject>(O));
			}
			Out->SetArrayField(TEXT("assets"), OutArr);
			Out->SetNumberField(TEXT("count"), OutArr.Num());
			return true;
		}

		if (Cmd == TEXT("asset.rename"))
		{
			FString From, To;
			if (!GetStr(Args, TEXT("from"), From) || !GetStr(Args, TEXT("to"), To)) { Err = TEXT("from/to required"); return false; }
			const bool bOk = UEditorAssetLibrary::RenameAsset(From, To);
			Out->SetBoolField(TEXT("renamed"), bOk);
			if (!bOk) { Err = TEXT("rename failed (exists or invalid path?)"); }
			return bOk;
		}
		if (Cmd == TEXT("asset.delete"))
		{
			FString Path;
			if (!GetStr(Args, TEXT("path"), Path)) { Err = Path; return false; }
			Out->SetBoolField(TEXT("deleted"), UEditorAssetLibrary::DeleteAsset(Path));
			return true;
		}

		// -------------------------------------------------------- blueprint
		if (Cmd == TEXT("bp.create"))
		{
			FString AssetPath, ParentClass = TEXT("Actor");
			if (!GetStr(Args, TEXT("path"), AssetPath)) { Err = AssetPath; return false; }
			Args->TryGetStringField(TEXT("parent"), ParentClass);

			FString PackagePath = FPackageName::GetLongPackagePath(AssetPath);
			FString AssetName = FPaths::GetBaseFilename(AssetPath);

			UClass* Parent = UClass::TryFindTypeSlow<UClass>(*ParentClass);
			if (!Parent)
			{
				Parent = UClass::TryFindTypeSlow<UClass>(*(TEXT("A") + ParentClass));
			}
			if (!Parent)
			{
				if (UObject* Found = LoadAny(ParentClass)) { Parent = Cast<UClass>(Found); }
			}
			if (!Parent) { Err = FString::Printf(TEXT("parent class not found: %s"), *ParentClass); return false; }

			UBlueprintFactory* Factory = NewObject<UBlueprintFactory>();
			Factory->ParentClass = Parent;
			IAssetTools& AssetTools = FModuleManager::LoadModuleChecked<FAssetToolsModule>(TEXT("AssetTools")).Get();
			UObject* Created = AssetTools.CreateAsset(AssetName, PackagePath, UBlueprint::StaticClass(), Factory);
			if (!Created) { Err = TEXT("asset creation failed"); return false; }
			Out->SetStringField(TEXT("path"), Created->GetPathName());
			return true;
		}

		if (Cmd == TEXT("bp.variable_add"))
		{
			FString Path, Name, Type = TEXT("bool");
			if (!GetStr(Args, TEXT("bp"), Path) || !GetStr(Args, TEXT("name"), Name)) { Err = TEXT("bp/name required"); return false; }
			Args->TryGetStringField(TEXT("type"), Type);
			UBlueprint* BP = LoadBlueprint(Path, Err);
			if (!BP) { return false; }

			FEdGraphPinType PinType;
			if (Type.Equals(TEXT("bool"), ESearchCase::IgnoreCase)) { PinType.PinCategory = UEdGraphSchema_K2::PC_Boolean; }
			else if (Type.StartsWith(TEXT("int"))) { PinType.PinCategory = UEdGraphSchema_K2::PC_Int; }
			else if (Type.StartsWith(TEXT("float")) || Type.StartsWith(TEXT("double"))) { PinType.PinCategory = UEdGraphSchema_K2::PC_Real; PinType.PinSubCategory = UEdGraphSchema_K2::PC_Double; }
			else if (Type.StartsWith(TEXT("string"))) { PinType.PinCategory = UEdGraphSchema_K2::PC_String; }
			else if (Type.StartsWith(TEXT("name"))) { PinType.PinCategory = UEdGraphSchema_K2::PC_Name; }
			else if (Type.StartsWith(TEXT("text"))) { PinType.PinCategory = UEdGraphSchema_K2::PC_Text; }
			else if (Type.StartsWith(TEXT("vector"))) { PinType.PinCategory = UEdGraphSchema_K2::PC_Struct; PinType.PinSubCategoryObject = TBaseStructure<FVector>::Get(); }
			else if (Type.StartsWith(TEXT("rotator"))) { PinType.PinCategory = UEdGraphSchema_K2::PC_Struct; PinType.PinSubCategoryObject = TBaseStructure<FRotator>::Get(); }
			else if (Type.StartsWith(TEXT("transform"))) { PinType.PinCategory = UEdGraphSchema_K2::PC_Struct; PinType.PinSubCategoryObject = TBaseStructure<FTransform>::Get(); }
			else
			{
				// treat as a class/struct/object reference path
				UClass* Cls = UClass::TryFindTypeSlow<UClass>(*Type);
				PinType.PinCategory = Cls ? UEdGraphSchema_K2::PC_Object : UEdGraphSchema_K2::PC_Struct;
				PinType.PinSubCategoryObject = Cls;
				if (!Cls) { Err = FString::Printf(TEXT("unsupported type: %s"), *Type); return false; }
			}

			FBlueprintEditorUtils::AddMemberVariable(BP, FName(*Name), PinType);
			FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(BP);
			Out->SetStringField(TEXT("name"), Name);
			return true;
		}

		if (Cmd == TEXT("bp.list_graphs"))
		{
			FString Path;
			if (!GetStr(Args, TEXT("bp"), Path)) { Err = Path; return false; }
			UBlueprint* BP = LoadBlueprint(Path, Err);
			if (!BP) { return false; }
			TArray<TSharedPtr<FJsonValue>> Graphs;
			auto AddGraphs = [&](const TArray<UEdGraph*>& List)
			{
				for (UEdGraph* G : List)
				{
					if (G) { Graphs.Add(MakeShared<FJsonValueString>(G->GetName())); }
				}
			};
			AddGraphs(BP->UbergraphPages);
			AddGraphs(BP->FunctionGraphs);
			AddGraphs(BP->MacroGraphs);
			Out->SetArrayField(TEXT("graphs"), Graphs);
			return true;
		}

		if (Cmd == TEXT("bp.graph_read"))
		{
			FString Path, GraphName;
			if (!GetStr(Args, TEXT("bp"), Path)) { Err = Path; return false; }
			Args->TryGetStringField(TEXT("graph"), GraphName);
			UBlueprint* BP = LoadBlueprint(Path, Err);
			if (!BP) { return false; }
			UEdGraph* Graph = FindGraph(BP, GraphName);
			if (!Graph) { Err = TEXT("graph not found"); return false; }

			TArray<TSharedPtr<FJsonValue>> Nodes;
			for (UEdGraphNode* N : Graph->Nodes)
			{
				if (N) { Nodes.Add(MakeShared<FJsonValueObject>(NodeToJson(N))); }
			}
			Out->SetArrayField(TEXT("nodes"), Nodes);
			Out->SetStringField(TEXT("graph"), Graph->GetName());
			return true;
		}

		if (Cmd == TEXT("bp.add_node"))
		{
			FString Path, GraphName, Kind;
			if (!GetStr(Args, TEXT("bp"), Path) || !GetStr(Args, TEXT("kind"), Kind)) { Err = TEXT("bp/kind required"); return false; }
			Args->TryGetStringField(TEXT("graph"), GraphName);
			UBlueprint* BP = LoadBlueprint(Path, Err);
			if (!BP) { return false; }
			UEdGraph* Graph = FindGraph(BP, GraphName);
			if (!Graph) { Err = TEXT("graph not found"); return false; }

			const int32 X = GetInt(Args, TEXT("x"), 0);
			const int32 Y = GetInt(Args, TEXT("y"), 0);

			UK2Node* NewNode = nullptr;

			if (Kind == TEXT("call_function"))
			{
				FString FunctionName, TargetClass;
				if (!GetStr(Args, TEXT("function"), FunctionName)) { Err = FunctionName; return false; }
				Args->TryGetStringField(TEXT("target_class"), TargetClass);

				UClass* Class = nullptr;
				if (!TargetClass.IsEmpty())
				{
					Class = UClass::TryFindTypeSlow<UClass>(*TargetClass);
					if (!Class) { Class = UClass::TryFindTypeSlow<UClass>(*(TEXT("U") + TargetClass)); }
					if (!Class) { Class = UClass::TryFindTypeSlow<UClass>(*(TEXT("A") + TargetClass)); }
				}
				else if (BP->ParentClass)
				{
					Class = BP->ParentClass;
				}
				UFunction* Fn = FindFunction(Class, FunctionName);
				if (!Fn && BP->GeneratedClass) { Fn = FindFunction(BP->GeneratedClass, FunctionName); }
				if (!Fn) { Err = FString::Printf(TEXT("function '%s' not found on %s"), *FunctionName, *GetNameSafe(Class)); return false; }

				UK2Node_CallFunction* CallNode = NewObject<UK2Node_CallFunction>(Graph);
				CallNode->SetFromFunction(Fn);
				CallNode->NodePosX = X;
				CallNode->NodePosY = Y;
				Graph->AddNode(CallNode, /*bUserAction*/ true, /*bSelectNewNode*/ false);
				CallNode->AllocateDefaultPins();
				NewNode = CallNode;
			}
			else if (Kind == TEXT("event"))
			{
				FString EventName;
				if (!GetStr(Args, TEXT("event"), EventName)) { Err = EventName; return false; }
				UFunction* Fn = FindFunction(BP->ParentClass, EventName);
				if (!Fn) { Err = FString::Printf(TEXT("event '%s' not found on parent"), *EventName); return false; }

				UK2Node_Event* EventNode = NewObject<UK2Node_Event>(Graph);
				EventNode->EventReference.SetExternalMember(Fn->GetFName(), Fn->GetOwnerClass());
				EventNode->bOverrideFunction = true;
				EventNode->NodePosX = X;
				EventNode->NodePosY = Y;
				Graph->AddNode(EventNode, true, false);
				EventNode->AllocateDefaultPins();
				NewNode = EventNode;
			}
			else if (Kind == TEXT("custom_event"))
			{
				FString Name;
				if (!GetStr(Args, TEXT("name"), Name)) { Err = Name; return false; }
				UK2Node_CustomEvent* Custom = NewObject<UK2Node_CustomEvent>(Graph);
				Custom->CustomFunctionName = FName(*Name);
				Custom->NodePosX = X;
				Custom->NodePosY = Y;
				Graph->AddNode(Custom, true, false);
				Custom->AllocateDefaultPins();
				NewNode = Custom;
			}
			else if (Kind == TEXT("variable_get") || Kind == TEXT("variable_set"))
			{
				FString VarName;
				if (!GetStr(Args, TEXT("variable"), VarName)) { Err = VarName; return false; }
				const FName VariableName(*VarName);
				const FProperty* Prop = FindFProperty<FProperty>(BP->GeneratedClass ? BP->GeneratedClass : BP->ParentClass, VariableName);
				if (!Prop && BP->GeneratedClass)
				{
					Prop = FindFProperty<FProperty>(BP->GeneratedClass, VariableName);
				}
				if (!Prop) { Err = FString::Printf(TEXT("variable '%s' not found (compile the blueprint first)"), *VarName); return false; }

				UK2Node_Variable* VarNode = nullptr;
				if (Kind == TEXT("variable_get"))
				{
					UK2Node_VariableGet* Get = NewObject<UK2Node_VariableGet>(Graph);
					Get->VariableReference.SetSelfMember(VariableName);
					VarNode = Get;
				}
				else
				{
					UK2Node_VariableSet* Set = NewObject<UK2Node_VariableSet>(Graph);
					Set->VariableReference.SetSelfMember(VariableName);
					VarNode = Set;
				}
				VarNode->NodePosX = X;
				VarNode->NodePosY = Y;
				Graph->AddNode(VarNode, true, false);
				VarNode->AllocateDefaultPins();
				NewNode = VarNode;
			}
			else if (Kind == TEXT("branch"))
			{
				UK2Node_IfThenElse* Branch = NewObject<UK2Node_IfThenElse>(Graph);
				Branch->NodePosX = X; Branch->NodePosY = Y;
				Graph->AddNode(Branch, true, false);
				Branch->AllocateDefaultPins();
				NewNode = Branch;
			}
			else if (Kind == TEXT("sequence"))
			{
				UK2Node_ExecutionSequence* Seq = NewObject<UK2Node_ExecutionSequence>(Graph);
				Seq->NodePosX = X; Seq->NodePosY = Y;
				Graph->AddNode(Seq, true, false);
				Seq->AllocateDefaultPins();
				NewNode = Seq;
			}
			else if (Kind == TEXT("cast"))
			{
				FString TargetClass;
				if (!GetStr(Args, TEXT("target_class"), TargetClass)) { Err = TargetClass; return false; }
				UClass* Cls = UClass::TryFindTypeSlow<UClass>(*TargetClass);
				if (!Cls) { Err = FString::Printf(TEXT("class not found: %s"), *TargetClass); return false; }
				UK2Node_DynamicCast* CastNode = NewObject<UK2Node_DynamicCast>(Graph);
				CastNode->TargetType = Cls;
				CastNode->NodePosX = X; CastNode->NodePosY = Y;
				Graph->AddNode(CastNode, true, false);
				CastNode->AllocateDefaultPins();
				NewNode = CastNode;
			}
			else
			{
				Err = FString::Printf(TEXT("unknown node kind: %s"), *Kind);
				return false;
			}

			FBlueprintEditorUtils::MarkBlueprintAsModified(BP);
			Out = NodeToJson(NewNode);
			return true;
		}

		if (Cmd == TEXT("bp.connect"))
		{
			FString Path, GraphName, FromSpec, ToSpec;
			if (!GetStr(Args, TEXT("bp"), Path) || !GetStr(Args, TEXT("from"), FromSpec) || !GetStr(Args, TEXT("to"), ToSpec))
			{
				Err = TEXT("bp/from/to required");
				return false;
			}
			Args->TryGetStringField(TEXT("graph"), GraphName);
			UBlueprint* BP = LoadBlueprint(Path, Err);
			if (!BP) { return false; }
			UEdGraph* Graph = FindGraph(BP, GraphName);
			if (!Graph) { Err = TEXT("graph not found"); return false; }

			FString E1, E2;
			UEdGraphPin* A = SplitPinSpec(Graph, FromSpec, EGPD_Output, E1);
			UEdGraphPin* B = SplitPinSpec(Graph, ToSpec, EGPD_Input, E2);
			if (!A) { Err = E1; return false; }
			if (!B) { Err = E2; return false; }

			const UEdGraphSchema* Schema = Graph->GetSchema();
			const bool bConnected = Schema->TryCreateConnection(A, B);
			if (!bConnected)
			{
				// Retry with coercion for numeric/struct conversions.
				Schema->TryCreateConnection(B, A);
			}
			FBlueprintEditorUtils::MarkBlueprintAsModified(BP);
			Out->SetBoolField(TEXT("connected"), bConnected);
			if (!bConnected) { Err = FString::Printf(TEXT("incompatible pins %s -> %s"), *A->PinType.PinCategory.ToString(), *B->PinType.PinCategory.ToString()); }
			return bConnected;
		}

		if (Cmd == TEXT("bp.disconnect"))
		{
			FString Path, GraphName, FromSpec, ToSpec;
			GetStr(Args, TEXT("bp"), Path); GetStr(Args, TEXT("from"), FromSpec); GetStr(Args, TEXT("to"), ToSpec, false);
			Args->TryGetStringField(TEXT("graph"), GraphName);
			UBlueprint* BP = LoadBlueprint(Path, Err);
			if (!BP) { return false; }
			UEdGraph* Graph = FindGraph(BP, GraphName);
			if (!Graph) { Err = TEXT("graph not found"); return false; }
			FString E1;
			UEdGraphPin* A = SplitPinSpec(Graph, FromSpec, EGPD_Output, E1);
			if (!A) { Err = E1; return false; }
			if (ToSpec.IsEmpty())
			{
				A->BreakAllPinLinks();
				FBlueprintEditorUtils::MarkBlueprintAsModified(BP);
				return true;
			}
			FString E2;
			UEdGraphPin* B = SplitPinSpec(Graph, ToSpec, EGPD_Input, E2);
			if (!B) { Err = E2; return false; }
			A->BreakLinkTo(B);
			FBlueprintEditorUtils::MarkBlueprintAsModified(BP);
			return true;
		}

		if (Cmd == TEXT("bp.set_pin_default"))
		{
			FString Path, GraphName, Spec, Value;
			GetStr(Args, TEXT("bp"), Path); GetStr(Args, TEXT("pin"), Spec); GetStr(Args, TEXT("value"), Value);
			Args->TryGetStringField(TEXT("graph"), GraphName);
			UBlueprint* BP = LoadBlueprint(Path, Err);
			if (!BP) { return false; }
			UEdGraph* Graph = FindGraph(BP, GraphName);
			if (!Graph) { Err = TEXT("graph not found"); return false; }
			FString E;
			UEdGraphPin* Pin = SplitPinSpec(Graph, Spec, EGPD_Input, E);
			if (!Pin) { Err = E; return false; }
			if (const UEdGraphSchema* PinSchema = Pin->GetSchema())
			{
				PinSchema->TrySetDefaultValue(*Pin, Value);
			}
			else
			{
				Pin->DefaultValue = Value;
			}
			FBlueprintEditorUtils::MarkBlueprintAsModified(BP);
			return true;
		}

		if (Cmd == TEXT("bp.compile"))
		{
			FString Path;
			if (!GetStr(Args, TEXT("bp"), Path)) { Err = Path; return false; }
			UBlueprint* BP = LoadBlueprint(Path, Err);
			if (!BP) { return false; }
			FKismetEditorUtilities::CompileBlueprint(BP, EBlueprintCompileOptions::SkipGarbageCollection);
			const bool bSuccess = (BP->Status == BS_UpToDate || BP->Status == BS_UpToDateWithWarnings);
			Out->SetBoolField(TEXT("success"), bSuccess);
			const TCHAR* StatusStr = TEXT("Unknown");
			switch (BP->Status)
			{
				case BS_Unknown:                  StatusStr = TEXT("Unknown"); break;
				case BS_BeingCreated:             StatusStr = TEXT("BeingCreated"); break;
				case BS_Dirty:                    StatusStr = TEXT("Dirty"); break;
				case BS_UpToDate:                 StatusStr = TEXT("UpToDate"); break;
				case BS_UpToDateWithWarnings:     StatusStr = TEXT("UpToDateWithWarnings"); break;
				case BS_Error:                    StatusStr = TEXT("Error"); break;
				default:                          StatusStr = TEXT("Unknown"); break;
			}
			Out->SetStringField(TEXT("status"), StatusStr);
			if (!bSuccess)
			{
				Err = TEXT("blueprint compile failed - read editor.log for details");
			}
			return bSuccess;
		}

		// ----------------------------------------------------------- actors
		if (Cmd == TEXT("actor.find"))
		{
			FString ClassName;
			Args->TryGetStringField(TEXT("class"), ClassName);
			const int32 Limit = GetInt(Args, TEXT("limit"), 200);
			UWorld* World = GEditor ? GEditor->GetEditorWorldContext().World() : nullptr;
			if (!World) { Err = TEXT("no editor world"); return false; }
			TArray<TSharedPtr<FJsonValue>> Arr;
			for (TActorIterator<AActor> It(World); It && Arr.Num() < Limit; ++It)
			{
				AActor* A = *It;
				if (!ClassName.IsEmpty() && !A->GetClass()->GetName().Contains(ClassName, ESearchCase::IgnoreCase)) { continue; }
				auto O = MakeShared<FJsonObject>();
				O->SetStringField(TEXT("name"), A->GetName());
				O->SetStringField(TEXT("class"), A->GetClass()->GetName());
				O->SetStringField(TEXT("path"), A->GetPathName());
				const FVector Loc = A->GetActorLocation();
				O->SetStringField(TEXT("location"), Loc.ToString());
				Arr.Add(MakeShared<FJsonValueObject>(O));
			}
			Out->SetArrayField(TEXT("actors"), Arr);
			return true;
		}

		if (Cmd == TEXT("actor.spawn"))
		{
			FString ClassName;
			if (!GetStr(Args, TEXT("class"), ClassName)) { Err = ClassName; return false; }
			UWorld* World = GEditor ? GEditor->GetEditorWorldContext().World() : nullptr;
			if (!World) { Err = TEXT("no editor world"); return false; }

			UClass* Cls = UClass::TryFindTypeSlow<UClass>(*ClassName);
			if (!Cls) { Cls = UClass::TryFindTypeSlow<UClass>(*(TEXT("A") + ClassName)); }
			if (!Cls)
			{
				// maybe a blueprint asset path
				if (UBlueprint* BP = Cast<UBlueprint>(LoadAny(ClassName))) { Cls = BP->GeneratedClass; }
			}
			if (!Cls) { Err = FString::Printf(TEXT("class not found: %s"), *ClassName); return false; }

			const FVector Loc(GetNum(Args, TEXT("x")), GetNum(Args, TEXT("y")), GetNum(Args, TEXT("z")));

			FActorSpawnParameters Params;
			Params.SpawnCollisionHandlingOverride = ESpawnActorCollisionHandlingMethod::AlwaysSpawn;
			AActor* Actor = World->SpawnActor<AActor>(Cls, Loc, FRotator::ZeroRotator, Params);
			if (!Actor) { Err = TEXT("spawn failed"); return false; }
			Out->SetStringField(TEXT("name"), Actor->GetName());
			Out->SetStringField(TEXT("path"), Actor->GetPathName());
			return true;
		}

		if (Cmd == TEXT("actor.set_property"))
		{
			FString ActorName, PropName, Value;
			if (!GetStr(Args, TEXT("actor"), ActorName) || !GetStr(Args, TEXT("property"), PropName) || !GetStr(Args, TEXT("value"), Value))
			{
				Err = TEXT("actor/property/value required");
				return false;
			}
			UWorld* World = GEditor ? GEditor->GetEditorWorldContext().World() : nullptr;
			for (TActorIterator<AActor> It(World); It; ++It)
			{
				if (It->GetName() != ActorName) { continue; }
				FProperty* Prop = FindFProperty<FProperty>(It->GetClass(), FName(*PropName));
				if (!Prop) { Err = FString::Printf(TEXT("property '%s' not found"), *PropName); return false; }
				It->Modify();
				void* Addr = Prop->ContainerPtrToValuePtr<void>(*It);
#if UE_VERSION_OLDER_THAN(5, 1, 0)
				const TCHAR* Imported = Prop->ImportText(*Value, Addr, PPF_None, It.operator->());
#else
				const TCHAR* Imported = Prop->ImportText_Direct(*Value, Addr, It.operator->(), PPF_None);
#endif
				if (!Imported)
				{
					Err = FString::Printf(TEXT("could not parse '%s' as %s"), *Value, *PropName);
					return false;
				}
				Out->SetBoolField(TEXT("set"), true);
				return true;
			}
			Err = FString::Printf(TEXT("actor not found: %s"), *ActorName);
			return false;
		}

		if (Cmd == TEXT("actor.call"))
		{
			FString ActorName, FnName;
			GetStr(Args, TEXT("actor"), ActorName); GetStr(Args, TEXT("function"), FnName);
			UWorld* World = GEditor ? GEditor->GetEditorWorldContext().World() : nullptr;
			for (TActorIterator<AActor> It(World); It; ++It)
			{
				if (It->GetName() != ActorName) { continue; }
				if (UFunction* Fn = FindFunction(It->GetClass(), FnName))
				{
					It->ProcessEvent(Fn, nullptr);
					return true;
				}
				Err = FString::Printf(TEXT("function '%s' not found"), *FnName);
				return false;
			}
			Err = TEXT("actor not found");
			return false;
		}

		// -------------------------------------------------------------- PIE
		if (Cmd == TEXT("pie.start"))
		{
			FRequestPlaySessionParams Params;
			GEditor->RequestPlaySession(Params);
			return true;
		}
		if (Cmd == TEXT("pie.stop"))
		{
			GEditor->RequestEndPlayMap();
			return true;
		}


		// ---------------------------------------------------- graph builder
		// Builds an entire node graph in ONE call / ONE transaction.
		// Nodes are addressed by symbolic "ref" names which are resolved to
		// GUIDs as we go, so the agent never has to round-trip just to learn ids.
		if (Cmd == TEXT("bp.build_graph"))
		{
			FString Path, GraphName;
			if (!GetStr(Args, TEXT("bp"), Path)) { Err = Path; return false; }
			Args->TryGetStringField(TEXT("graph"), GraphName);
			UBlueprint* BP = LoadBlueprint(Path, Err);
			if (!BP) { return false; }
			UEdGraph* Graph = FindGraph(BP, GraphName);
			if (!Graph) { Err = TEXT("graph not found"); return false; }

			const TArray<TSharedPtr<FJsonValue>>* NodeSpecs = nullptr;
			Args->TryGetArrayField(TEXT("nodes"), NodeSpecs);
			TMap<FString, FString> Refs;      // ref -> node guid
			TArray<TSharedPtr<FJsonValue>> NodeResults;
			TArray<TSharedPtr<FJsonValue>> Errors;

			if (NodeSpecs)
			{
				for (const TSharedPtr<FJsonValue>& V : *NodeSpecs)
				{
					TSharedPtr<FJsonObject> Spec = V->AsObject();
					if (!Spec.IsValid()) { continue; }

					// Copy the spec, minus the harness-only "ref" key, and inject the
					// blueprint + graph so each node verb is self-contained.
					TSharedPtr<FJsonObject> NodeArgs = MakeShared<FJsonObject>();
					for (const auto& Pair : Spec->Values)
					{
						if (Pair.Key != TEXT("ref"))
						{
							NodeArgs->SetField(Pair.Key, Pair.Value);
						}
					}
					NodeArgs->SetStringField(TEXT("bp"), Path);
					if (!GraphName.IsEmpty())
					{
						NodeArgs->SetStringField(TEXT("graph"), GraphName);
					}

					FString Ref;
					Spec->TryGetStringField(TEXT("ref"), Ref);

					FString NodeErr;
					TSharedPtr<FJsonObject> NodeOut = MakeShared<FJsonObject>();
					if (Dispatch(TEXT("bp.add_node"), NodeArgs, NodeOut, NodeErr))
					{
						FString Guid;
						NodeOut->TryGetStringField(TEXT("id"), Guid);
						if (!Ref.IsEmpty()) { Refs.Add(Ref, Guid); }
						auto Rec = MakeShared<FJsonObject>();
						Rec->SetStringField(TEXT("ref"), Ref);
						Rec->SetStringField(TEXT("id"), Guid);
						Rec->SetStringField(TEXT("title"), NodeOut->GetStringField(TEXT("title")));
						NodeResults.Add(MakeShared<FJsonValueObject>(Rec));
					}
					else
					{
						auto E = MakeShared<FJsonObject>();
						E->SetStringField(TEXT("ref"), Ref);
						E->SetStringField(TEXT("error"), NodeErr);
						Errors.Add(MakeShared<FJsonValueObject>(E));
					}
				}
			}

			// Resolve "ref.pin" -> "guid:pin"
			auto ResolveSpec = [&Refs](FString Spec) -> FString
			{
				FString Ref, Pin;
				if (!Spec.Split(TEXT("."), &Ref, &Pin)) { return Spec; }
				if (const FString* Guid = Refs.Find(Ref)) { return *Guid + TEXT(":") + Pin; }
				return Spec;
			};

			const TArray<TSharedPtr<FJsonValue>>* LinkSpecs = nullptr;
			Args->TryGetArrayField(TEXT("links"), LinkSpecs);
			TArray<TSharedPtr<FJsonValue>> LinkResults;
			if (LinkSpecs)
			{
				for (const TSharedPtr<FJsonValue>& V : *LinkSpecs)
				{
					TSharedPtr<FJsonObject> Spec = V->AsObject();
					if (!Spec.IsValid()) { continue; }
					FString FromSpec, ToSpec;
					Spec->TryGetStringField(TEXT("from"), FromSpec);
					Spec->TryGetStringField(TEXT("to"), ToSpec);

					auto LinkArgs = MakeShared<FJsonObject>();
					LinkArgs->SetStringField(TEXT("bp"), Path);
					LinkArgs->SetStringField(TEXT("graph"), GraphName);
					LinkArgs->SetStringField(TEXT("from"), ResolveSpec(FromSpec));
					LinkArgs->SetStringField(TEXT("to"), ResolveSpec(ToSpec));

					FString LinkErr;
					TSharedPtr<FJsonObject> LinkOut = MakeShared<FJsonObject>();
					const bool bLinked = Dispatch(TEXT("bp.connect"), LinkArgs, LinkOut, LinkErr);
					auto Rec = MakeShared<FJsonObject>();
					Rec->SetStringField(TEXT("from"), FromSpec);
					Rec->SetStringField(TEXT("to"), ToSpec);
					Rec->SetBoolField(TEXT("ok"), bLinked);
					if (!bLinked) { Rec->SetStringField(TEXT("error"), LinkErr); }
					LinkResults.Add(MakeShared<FJsonValueObject>(Rec));
				}
			}

			// Optional pin defaults: [{"pin":"ref.pin","value":"hello"}]
			const TArray<TSharedPtr<FJsonValue>>* DefaultSpecs = nullptr;
			Args->TryGetArrayField(TEXT("defaults"), DefaultSpecs);
			if (DefaultSpecs)
			{
				for (const TSharedPtr<FJsonValue>& V : *DefaultSpecs)
				{
					TSharedPtr<FJsonObject> Spec = V->AsObject();
					if (!Spec.IsValid()) { continue; }
					FString PinSpec, Value;
					Spec->TryGetStringField(TEXT("pin"), PinSpec);
					Spec->TryGetStringField(TEXT("value"), Value);
					auto A = MakeShared<FJsonObject>();
					A->SetStringField(TEXT("bp"), Path);
					A->SetStringField(TEXT("graph"), GraphName);
					A->SetStringField(TEXT("pin"), ResolveSpec(PinSpec));
					A->SetStringField(TEXT("value"), Value);
					FString E;
					TSharedPtr<FJsonObject> O = MakeShared<FJsonObject>();
					Dispatch(TEXT("bp.set_pin_default"), A, O, E);
				}
			}

			bool bCompiled = false;
			if (GetBool(Args, TEXT("compile"), true))
			{
				auto A = MakeShared<FJsonObject>();
				A->SetStringField(TEXT("bp"), Path);
				FString E;
				TSharedPtr<FJsonObject> O = MakeShared<FJsonObject>();
				bCompiled = Dispatch(TEXT("bp.compile"), A, O, E);
				if (!bCompiled) { Out->SetStringField(TEXT("compile_error"), E); }
			}

			Out->SetArrayField(TEXT("nodes"), NodeResults);
			Out->SetArrayField(TEXT("links"), LinkResults);
			Out->SetArrayField(TEXT("errors"), Errors);
			Out->SetBoolField(TEXT("compiled"), bCompiled);
			return Errors.Num() == 0;
		}

		// --------------------------------------------------------------- umg
		if (Cmd == TEXT("umg.create_widget"))
		{
			FString Path, TitleText;
			if (!GetStr(Args, TEXT("path"), Path)) { Err = Path; return false; }
			Args->TryGetStringField(TEXT("title_text"), TitleText);

			const FString PackageName = FPackageName::ObjectPathToPackageName(Path);
			const FString AssetName = FPackageName::GetLongPackageAssetName(PackageName);
			if (PackageName.IsEmpty() || AssetName.IsEmpty())
			{
				Err = FString::Printf(TEXT("invalid widget asset path: %s"), *Path);
				return false;
			}

			UWidgetBlueprint* WBP = nullptr;
			bool bCreated = false;
			if (UEditorAssetLibrary::DoesAssetExist(PackageName))
			{
				WBP = Cast<UWidgetBlueprint>(UEditorAssetLibrary::LoadAsset(PackageName));
				if (!WBP)
				{
					Err = FString::Printf(TEXT("asset exists at %s and is not a WidgetBlueprint"), *PackageName);
					return false;
				}
			}
			else
			{
				UPackage* Pkg = CreatePackage(*PackageName);
				if (!Pkg)
				{
					Err = FString::Printf(TEXT("failed to create package %s"), *PackageName);
					return false;
				}
				WBP = Cast<UWidgetBlueprint>(FKismetEditorUtilities::CreateBlueprint(
					UUserWidget::StaticClass(),
					Pkg,
					FName(*AssetName),
					BPTYPE_Normal,
					UWidgetBlueprint::StaticClass(),
					UWidgetBlueprintGeneratedClass::StaticClass(),
					NAME_None));
				if (!WBP)
				{
					Err = FString::Printf(TEXT("failed to create WidgetBlueprint %s"), *PackageName);
					return false;
				}
				FAssetRegistryModule::AssetCreated(WBP);
				Pkg->MarkPackageDirty();
				bCreated = true;
			}

			if (WBP->WidgetTree)
			{
				UPanelWidget* RootPanel = Cast<UPanelWidget>(WBP->WidgetTree->RootWidget);
				if (!RootPanel)
				{
					UCanvasPanel* RootCanvas = WBP->WidgetTree->ConstructWidget<UCanvasPanel>(
						UCanvasPanel::StaticClass(), TEXT("RootCanvas"));
					WBP->WidgetTree->RootWidget = RootCanvas;
					RootPanel = RootCanvas;
				}

				if (!TitleText.IsEmpty() && RootPanel)
				{
					UTextBlock* TitleWidget = WBP->WidgetTree->FindWidget<UTextBlock>(TEXT("TitleText"));
					if (!TitleWidget)
					{
						TitleWidget = WBP->WidgetTree->ConstructWidget<UTextBlock>(
							UTextBlock::StaticClass(), TEXT("TitleText"));
						UPanelSlot* PanelSlot = RootPanel->AddChild(TitleWidget);
						if (UCanvasPanelSlot* CanvasSlot = Cast<UCanvasPanelSlot>(PanelSlot))
						{
							CanvasSlot->SetAnchors(FAnchors(0.5f, 0.2f, 0.5f, 0.2f));
							CanvasSlot->SetAlignment(FVector2D(0.5f, 0.5f));
							CanvasSlot->SetAutoSize(true);
						}
					}
					if (TitleWidget)
					{
						TitleWidget->SetText(FText::FromString(TitleText));
					}
				}
			}

			FBlueprintEditorUtils::MarkBlueprintAsModified(WBP);
			FKismetEditorUtilities::CompileBlueprint(WBP, EBlueprintCompileOptions::SkipGarbageCollection);
			UEditorAssetLibrary::SaveAsset(PackageName, false);

			Out->SetStringField(TEXT("path"), PackageName);
			Out->SetBoolField(TEXT("created"), bCreated);
			Out->SetBoolField(TEXT("existed"), !bCreated);
			if (!TitleText.IsEmpty())
			{
				Out->SetStringField(TEXT("title_text"), TitleText);
			}
			return true;
		}

		// ------------------------------------------------------------ python
		// Escape hatch: anything the native surface doesn't cover yet can be
		// done with the full Unreal Python API, still over the fast socket.
		if (Cmd == TEXT("python.exec"))
		{
			FString Code;
			if (!GetStr(Args, TEXT("code"), Code)) { Err = Code; return false; }
			if (!IPythonScriptPlugin::Get() || !IPythonScriptPlugin::Get()->IsPythonAvailable())
			{
				Err = TEXT("python plugin unavailable");
				return false;
			}
			FPythonCommandEx CmdEx;
			CmdEx.Command = Code;
			CmdEx.ExecutionMode = EPythonCommandExecutionMode::ExecuteFile;
			CmdEx.FileExecutionScope = EPythonFileExecutionScope::Private;
			const bool bOk = IPythonScriptPlugin::Get()->ExecPythonCommandEx(CmdEx);
			Out->SetStringField(TEXT("output"), CmdEx.CommandResult + TEXT("\n") + CmdEx.LogOutput);
			if (!bOk)
			{
				Err = CmdEx.CommandResult.IsEmpty() ? TEXT("python error") : CmdEx.CommandResult;
			}
			return bOk;
		}

		Err = FString::Printf(TEXT("unknown command: %s"), *Cmd);
		return false;
	}
}
