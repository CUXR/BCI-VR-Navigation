using UnityEngine;

/// <summary>
/// Forces the XR Origin to a specific world position when the scene starts.
/// Attach this to the XR Origin root GameObject (NOT the Main Camera).
///
/// Set 'Start Position' in the Inspector to the world-space X/Y/Z you want
/// the player to begin at, then press Play — the rig will be placed there
/// before any XR tracking runs.
/// </summary>
public class XROriginSpawnPoint : MonoBehaviour
{
    [Tooltip("World-space position the XR Origin will be moved to on Awake. " +
             "Set this to where the player should start in the scene.")]
    public Vector3 startPosition;

    [Tooltip("Y rotation (degrees) the rig faces on start. 0 = world forward.")]
    public float startYRotation = 0f;

    private void Awake()
    {
        transform.position = startPosition;
        transform.rotation = Quaternion.Euler(0f, startYRotation, 0f);
    }
}
