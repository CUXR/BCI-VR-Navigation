using System;
using System.Linq;
using System.Threading;
using UnityEditor;
using UnityEditor.PackageManager;
using UnityEditor.XR.Management;
using UnityEditor.XR.Management.Metadata;
using UnityEngine;

/// Moves the Quest build from the Oculus XR plugin, which Unity 6.6 reports as no longer
/// supported, to OpenXR with the Meta Quest feature.
///
/// Run in two batch passes, because the OpenXR types only exist after the package compiles:
///   1. -executeMethod QuestOpenXRSetup.InstallPackages
///   2. -executeMethod QuestOpenXRSetup.Configure
public static class QuestOpenXRSetup
{
    const string OpenXRPackage = "com.unity.xr.openxr";
    const string OculusPackage = "com.unity.xr.oculus";
    const string OpenXRLoader = "UnityEngine.XR.OpenXR.OpenXRLoader";
    const string OculusLoader = "Unity.XR.Oculus.OculusLoader";

    public static void InstallPackages()
    {
        Wait(Client.Add(OpenXRPackage), "add " + OpenXRPackage);
    }

    public static void Configure()
    {
        var settings = XRGeneralSettingsPerBuildTarget.XRGeneralSettingsForBuildTarget(BuildTargetGroup.Android);
        if (settings == null || settings.Manager == null)
            throw new Exception("No Android XR settings found.");

        XRPackageMetadataStore.RemoveLoader(settings.Manager, OculusLoader, BuildTargetGroup.Android);
        if (!XRPackageMetadataStore.AssignLoader(settings.Manager, OpenXRLoader, BuildTargetGroup.Android))
            throw new Exception("Could not assign the OpenXR loader for Android.");
        settings.InitManagerOnStart = true;

        // OpenXR types are reached by reflection so this file compiles before the package exists.
        var settingsType = AppDomain.CurrentDomain.GetAssemblies()
            .Select(a => a.GetType("UnityEngine.XR.OpenXR.OpenXRSettings", false))
            .FirstOrDefault(t => t != null);
        if (settingsType == null) throw new Exception("OpenXR is not installed; run InstallPackages first.");
        var getSettings = settingsType.GetMethod("GetSettingsForBuildTargetGroup", new[] { typeof(BuildTargetGroup) });
        var openxr = getSettings?.Invoke(null, new object[] { BuildTargetGroup.Android }) as UnityEngine.Object;
        if (openxr == null) throw new Exception("OpenXR settings for Android not found.");
        // Populate the Android feature list; it is empty if Android support arrived after OpenXR.
        var helpers = AppDomain.CurrentDomain.GetAssemblies()
            .Select(a => a.GetType("UnityEditor.XR.OpenXR.Features.FeatureHelpers", false))
            .FirstOrDefault(t => t != null);
        helpers?.GetMethod("RefreshFeatures", new[] { typeof(BuildTargetGroup) })?.Invoke(null, new object[] { BuildTargetGroup.Android });
        var getFeatures = settingsType.GetMethods().First(m => m.Name == "GetFeatures" && !m.IsGenericMethodDefinition && m.GetParameters().Length == 0);
        var features = (Array)getFeatures.Invoke(openxr, null);
        Debug.Log("[QuestOpenXRSetup] Android features: " + string.Join(", ", features.Cast<object>().Select(f => f.GetType().Name)));
        int enabled = 0;
        foreach (var feature in features)
        {
            string type = feature.GetType().FullName;
            bool want = type == "UnityEngine.XR.OpenXR.Features.MetaQuestSupport.MetaQuestFeature"
                     || type == "UnityEngine.XR.OpenXR.Features.Interactions.OculusTouchControllerProfile"
                     || type == "UnityEngine.XR.OpenXR.Features.Interactions.MetaQuestTouchPlusControllerProfile";
            if (!want) continue;
            feature.GetType().GetProperties().First(p => p.Name == "enabled" && p.CanWrite && p.DeclaringType.Name == "OpenXRFeature").SetValue(feature, true);
            EditorUtility.SetDirty((UnityEngine.Object)feature);
            enabled++;
        }
        if (enabled < 2) throw new Exception("Expected the Meta Quest feature and a Touch controller profile; enabled " + enabled + ".");

        EditorUtility.SetDirty(settings);
        EditorUtility.SetDirty(settings.Manager);
        EditorUtility.SetDirty(openxr);
        AssetDatabase.SaveAssets();

        var loaders = string.Join(", ", settings.Manager.activeLoaders.Select(l => l.GetType().Name));
        Debug.Log("[QuestOpenXRSetup] Android loaders: " + loaders + "; enabled OpenXR features: " + enabled);

        // The Oculus plugin is no longer referenced by any loader; remove it so it cannot run.
        Wait(Client.Remove(OculusPackage), "remove " + OculusPackage);
    }

    static void Wait(UnityEditor.PackageManager.Requests.Request request, string what)
    {
        while (!request.IsCompleted) Thread.Sleep(200);
        if (request.Status != StatusCode.Success)
            throw new Exception("Package Manager failed to " + what + ": " + request.Error?.message);
        Debug.Log("[QuestOpenXRSetup] " + what + " done.");
    }
}
