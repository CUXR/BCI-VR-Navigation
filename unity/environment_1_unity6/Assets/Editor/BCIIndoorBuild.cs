using System;
using System.IO;
using UnityEditor;
using UnityEditor.Build.Reporting;
using UnityEngine;

public static class BCIIndoorBuild
{
    public static void Build()
    {
        string output = Path.GetFullPath(Path.Combine(Application.dataPath, "../Build/BCIIndoor.apk"));
        Directory.CreateDirectory(Path.GetDirectoryName(output));
        var result = BuildPipeline.BuildPlayer(new BuildPlayerOptions
        {
            scenes = new[] { "Assets/Scenes/FreeNavScene.unity" },
            locationPathName = output,
            target = BuildTarget.Android,
            options = BuildOptions.Development | BuildOptions.AllowDebugging,
        });
        if (result.summary.result != BuildResult.Succeeded)
        {
            throw new Exception("Indoor Quest build failed: " + result.summary.result);
        }
        Debug.Log("Indoor Quest APK: " + output);
    }

    /// Desktop build for Apple Silicon and Intel Macs, controlled by DesktopNavigation.
    public static void Mac()
    {
        string output = Path.GetFullPath(Path.Combine(Application.dataPath, "../Build/BCIIndoor.app"));
        Directory.CreateDirectory(Path.GetDirectoryName(output));
        UnityEditor.OSXStandalone.UserBuildSettings.architecture = UnityEditor.Build.OSArchitecture.x64ARM64;

        // Standalone XR is configured to start the Mock HMD loader, which throws on a Mac with no
        // headset. The desktop build ships with no Standalone XR loaders and XR startup off. The
        // settings asset's original bytes are written back afterwards, so editor play mode and
        // the repository keep the XR configuration unchanged.
        var xr = UnityEditor.XR.Management.XRGeneralSettingsPerBuildTarget.XRGeneralSettingsForBuildTarget(BuildTargetGroup.Standalone);
        string xrAssetPath = xr != null ? AssetDatabase.GetAssetPath(xr) : null;
        byte[] xrAssetBytes = xrAssetPath != null ? File.ReadAllBytes(xrAssetPath) : null;
        var manager = xr != null ? xr.Manager : null;
        var loaders = manager != null ? new System.Collections.Generic.List<UnityEngine.XR.Management.XRLoader>(manager.activeLoaders) : new System.Collections.Generic.List<UnityEngine.XR.Management.XRLoader>();
        if (xr != null) xr.InitManagerOnStart = false;
        foreach (var l in loaders) manager.TryRemoveLoader(l);
        Debug.Log("[BCIIndoorBuild] Standalone XR for Mac build: init=" + (xr != null && xr.InitManagerOnStart) + " loaders=" + (manager != null ? manager.activeLoaders.Count : -1) + " (removed " + loaders.Count + ")");
        BuildReport report;
        try
        {
            report = BuildPipeline.BuildPlayer(new BuildPlayerOptions
            {
                scenes = new[] { "Assets/Scenes/FreeNavScene.unity" },
                locationPathName = output,
                target = BuildTarget.StandaloneOSX,
                options = BuildOptions.None,
            });
        }
        finally
        {
            if (xrAssetBytes != null)
            {
                File.WriteAllBytes(xrAssetPath, xrAssetBytes);
                AssetDatabase.ImportAsset(xrAssetPath, ImportAssetOptions.ForceUpdate);
            }
        }
        var result = report;
        if (result.summary.result != BuildResult.Succeeded)
        {
            throw new Exception("Indoor Mac build failed: " + result.summary.result);
        }
        Debug.Log("Indoor Mac app: " + output);
    }
}
