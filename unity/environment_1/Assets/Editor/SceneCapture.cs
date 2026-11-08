using System.IO;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;

/// Renders review screenshots of FreeNavScene from fixed viewpoints. Batch-mode entry point:
/// Unity -batchmode -quit -projectPath . -executeMethod SceneCapture.CaptureAll -captureDir <dir>
public static class SceneCapture
{
    struct View { public string name; public Vector3 pos; public Vector3 euler; public bool ortho; public float size; }

    static readonly View[] Views =
    {
        new View { name = "00_topdown", pos = new Vector3(-9f, 30f, -1f), euler = new Vector3(90f, 0f, 0f), ortho = true, size = 11f },
        new View { name = "01_spawn_north", pos = new Vector3(0f, 1.6f, -0.5f), euler = new Vector3(5f, 0f, 0f) },
        new View { name = "02_spawn_south", pos = new Vector3(0f, 1.6f, 0.5f), euler = new Vector3(5f, 180f, 0f) },
        new View { name = "03_backhall_west", pos = new Vector3(1.5f, 1.6f, -6f), euler = new Vector3(5f, 270f, 0f) },
        new View { name = "04_westcorner_north", pos = new Vector3(-12f, 1.6f, -8f), euler = new Vector3(5f, 0f, 0f) },
        new View { name = "05_northend_south", pos = new Vector3(0f, 1.6f, 6.5f), euler = new Vector3(5f, 180f, 0f) },
        new View { name = "06_wing_west", pos = new Vector3(-8.5f, 1.6f, 2.5f), euler = new Vector3(5f, 290f, 0f) },
        new View { name = "07_pantry_north", pos = new Vector3(-12f, 1.6f, -6.5f), euler = new Vector3(5f, 20f, 0f) },
        new View { name = "08_bakery_east", pos = new Vector3(-0.5f, 1.6f, -4f), euler = new Vector3(8f, 70f, 0f) },
        new View { name = "09_cafe_north", pos = new Vector3(-13f, 1.6f, 2.2f), euler = new Vector3(8f, 340f, 0f) },
    };

    public static void CaptureAll()
    {
        string dir = "Captures";
        var args = System.Environment.GetCommandLineArgs();
        for (int i = 0; i < args.Length - 1; i++) if (args[i] == "-captureDir") dir = args[i + 1];
        Directory.CreateDirectory(dir);

        EditorSceneManager.OpenScene("Assets/Scenes/FreeNavScene.unity");
        var go = new GameObject("CaptureCam");
        var cam = go.AddComponent<Camera>();
        cam.nearClipPlane = 0.05f; cam.farClipPlane = 200f; cam.fieldOfView = 75f;
        cam.clearFlags = CameraClearFlags.Skybox;
        var rt = new RenderTexture(1600, 1000, 24);
        cam.targetTexture = rt;
        var tex = new Texture2D(1600, 1000, TextureFormat.RGB24, false);

        foreach (var v in Views)
        {
            // The top-down view must see through the roof.
            var hidden = new System.Collections.Generic.List<Renderer>();
            if (v.ortho)
                foreach (var r in Object.FindObjectsOfType<Renderer>())
                    if (r.enabled && r.bounds.min.y > 2.9f) { r.enabled = false; hidden.Add(r); }

            cam.orthographic = v.ortho; cam.orthographicSize = v.size;
            cam.aspect = 1.6f;
            go.transform.SetPositionAndRotation(v.pos, Quaternion.Euler(v.euler));
            cam.Render();
            RenderTexture.active = rt;
            tex.ReadPixels(new Rect(0, 0, 1600, 1000), 0, 0);
            tex.Apply();
            File.WriteAllBytes(Path.Combine(dir, v.name + ".png"), tex.EncodeToPNG());
            foreach (var r in hidden) r.enabled = true;
        }
        RenderTexture.active = null;
        Object.DestroyImmediate(go);
    }
}
