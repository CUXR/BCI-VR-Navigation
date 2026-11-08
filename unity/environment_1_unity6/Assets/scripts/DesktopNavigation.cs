using UnityEngine;
using UnityEngine.XR;

/// Keyboard and mouse navigation for desktop builds, where no XR headset is running.
/// WASD or arrow keys move, clicking the window captures the mouse to look around, Escape
/// releases it. Added at startup only when no XR display is active, so the Quest build is
/// unaffected. BCI predictions from FreeNavController keep working alongside it.
public class DesktopNavigation : MonoBehaviour
{
    const float EyeHeight = 1.6f;
    const float MoveSpeed = 1.5f;
    const float TurnSpeedKeys = 90f;
    const float MouseSensitivity = 2.0f;

    Transform rig;
    Camera cam;
    CharacterController controller;
    float pitch;

    [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.AfterSceneLoad)]
    static void Install()
    {
        if (Application.isMobilePlatform || XRSettings.isDeviceActive) return;
        var origin = GameObject.Find("XR Origin");
        if (origin == null || origin.GetComponent<DesktopNavigation>() != null) return;
        origin.AddComponent<DesktopNavigation>();
    }

    void Start()
    {
        rig = transform;
        cam = Camera.main;

        // The device simulator and tracked pose drivers would fight the desktop camera.
        var simulator = GameObject.Find("XR Device Simulator");
        if (simulator != null) simulator.SetActive(false);
        // Hand controllers and their ray lines have nothing to track on a desktop.
        foreach (var hand in new[] { "LeftHand Controller", "RightHand Controller" })
        {
            var go = GameObject.Find(hand);
            if (go != null) go.SetActive(false);
        }
        foreach (var b in cam.GetComponents<Behaviour>())
            if (b.GetType().Name.Contains("TrackedPoseDriver")) b.enabled = false;

        // Stand on the floor with a walking-height eye, and collide with walls and props.
        rig.position = new Vector3(rig.position.x, 0f, rig.position.z);
        controller = rig.GetComponent<CharacterController>();
        if (controller == null)
        {
            controller = rig.gameObject.AddComponent<CharacterController>();
            controller.height = 1.7f;
            controller.radius = 0.25f;
            controller.center = new Vector3(0f, 0.87f, 0f);
            controller.stepOffset = 0.1f;
        }
        cam.transform.localRotation = Quaternion.identity;
    }

    void Update()
    {
        if (Input.GetMouseButtonDown(0)) { Cursor.lockState = CursorLockMode.Locked; Cursor.visible = false; }
        if (Input.GetKeyDown(KeyCode.Escape)) { Cursor.lockState = CursorLockMode.None; Cursor.visible = true; }

        float yaw = 0f;
        if (Cursor.lockState == CursorLockMode.Locked)
        {
            yaw += Input.GetAxis("Mouse X") * MouseSensitivity;
            pitch = Mathf.Clamp(pitch - Input.GetAxis("Mouse Y") * MouseSensitivity, -70f, 70f);
        }
        if (Input.GetKey(KeyCode.Q)) yaw -= TurnSpeedKeys * Time.deltaTime;
        if (Input.GetKey(KeyCode.E)) yaw += TurnSpeedKeys * Time.deltaTime;
        rig.Rotate(0f, yaw, 0f, Space.World);

        float forward = (Input.GetKey(KeyCode.W) || Input.GetKey(KeyCode.UpArrow) ? 1f : 0f) - (Input.GetKey(KeyCode.S) || Input.GetKey(KeyCode.DownArrow) ? 1f : 0f);
        float strafe = (Input.GetKey(KeyCode.D) || Input.GetKey(KeyCode.RightArrow) ? 1f : 0f) - (Input.GetKey(KeyCode.A) || Input.GetKey(KeyCode.LeftArrow) ? 1f : 0f);
        var move = (rig.forward * forward + rig.right * strafe);
        if (move.sqrMagnitude > 1f) move.Normalize();
        controller.Move((move * MoveSpeed + Vector3.down * 2f) * Time.deltaTime);
    }

    void LateUpdate()
    {
        // Keep the eye at walking height and apply mouse pitch on the camera only.
        var parent = cam.transform.parent;
        float parentY = parent != null ? parent.position.y : rig.position.y;
        cam.transform.localPosition = new Vector3(0f, rig.position.y + EyeHeight - parentY, 0f);
        cam.transform.localRotation = Quaternion.Euler(pitch, 0f, 0f);
    }
}
