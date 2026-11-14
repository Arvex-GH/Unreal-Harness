#pragma once

// Small, dependency-light JSON writing helper.
//
// We deliberately do not use TJsonWriter's field-serialisation overloads here:
// their exact signatures moved between 5.1 and 5.6 and this file is on the hot
// path of every reply. A hand-rolled writer is ~20 lines and stable everywhere.

#include "CoreMinimal.h"
#include "Dom/JsonObject.h"
#include "Dom/JsonValue.h"

namespace UEHarnessJson
{
	inline void EscapeInto(const FString& In, FString& Out)
	{
		for (int32 i = 0; i < In.Len(); ++i)
		{
			const TCHAR C = In[i];
			switch (C)
			{
			case TEXT('"'):  Out += TEXT("\\\""); break;
			case TEXT('\\'): Out += TEXT("\\\\"); break;
			case TEXT('\n'): Out += TEXT("\\n"); break;
			case TEXT('\r'): Out += TEXT("\\r"); break;
			case TEXT('\t'): Out += TEXT("\\t"); break;
			default:
				if (C < 0x20)
				{
					Out += FString::Printf(TEXT("\\u%04x"), C);
				}
				else
				{
					Out.AppendChar(C);
				}
			}
		}
	}

	inline void WriteValue(const TSharedPtr<FJsonValue>& Value, FString& Out);

	inline void WriteObject(const TSharedPtr<FJsonObject>& Obj, FString& Out)
	{
		Out += TEXT("{");
		bool bFirst = true;
		if (Obj.IsValid())
		{
			for (const TPair<FString, TSharedPtr<FJsonValue>>& Pair : Obj->Values)
			{
				if (!bFirst) { Out += TEXT(","); }
				bFirst = false;
				Out += TEXT("\"");
				EscapeInto(Pair.Key, Out);
				Out += TEXT("\":");
				WriteValue(Pair.Value, Out);
			}
		}
		Out += TEXT("}");
	}

	inline void WriteValue(const TSharedPtr<FJsonValue>& Value, FString& Out)
	{
		if (!Value.IsValid() || Value->Type == EJson::Null)
		{
			Out += TEXT("null");
			return;
		}
		switch (Value->Type)
		{
		case EJson::String:
			Out += TEXT("\"");
			EscapeInto(Value->AsString(), Out);
			Out += TEXT("\"");
			break;
		case EJson::Number:
			Out += FString::Printf(TEXT("%.17g"), Value->AsNumber());
			break;
		case EJson::Boolean:
			Out += Value->AsBool() ? TEXT("true") : TEXT("false");
			break;
		case EJson::Object:
			WriteObject(Value->AsObject(), Out);
			break;
		case EJson::Array:
		{
			Out += TEXT("[");
			bool bFirst = true;
			for (const TSharedPtr<FJsonValue>& Item : Value->AsArray())
			{
				if (!bFirst) { Out += TEXT(","); }
				bFirst = false;
				WriteValue(Item, Out);
			}
			Out += TEXT("]");
			break;
		}
		default:
			Out += TEXT("null");
		}
	}

	/** Serialise a result object into {"id":N,"ok":true,"result":{...}}. */
	inline FString MakeReply(int64 Id, bool bOk, const TSharedPtr<FJsonObject>& Result, const FString& Error)
	{
		FString Out;
		Out.Reserve(512);
		Out += FString::Printf(TEXT("{\"id\":%lld,\"ok\":%s"), Id, bOk ? TEXT("true") : TEXT("false"));
		if (bOk)
		{
			Out += TEXT(",\"result\":");
			WriteObject(Result, Out);
		}
		else
		{
			Out += TEXT(",\"error\":\"");
			EscapeInto(Error, Out);
			Out += TEXT("\"");
		}
		Out += TEXT("}");
		return Out;
	}
}
