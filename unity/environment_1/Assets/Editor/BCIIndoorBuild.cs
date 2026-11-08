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
}
