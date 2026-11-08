using System.Collections;
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.UI;

public class AutoMoveController : MonoBehaviour
{
    [Header("Path")]
    [Tooltip("Movement sequence executed in order.")]
    public List<MoveStep> steps = new List<MoveStep>();

    [Tooltip("Pause time after each move step.")]
    [Min(0f)]
    public float pauseBetweenSteps = 0.5f;

    [Header("Movement")]
    [Tooltip("Optional. If assigned, movement uses CharacterController.Move.")]
    public CharacterController characterController;

    [Header("Instruction UI")]
    [Tooltip("World-space canvas that appears in front of the player.")]
    public Transform instructionCanvas;

    [Tooltip("Text component used for instructions and countdown.")]
    public Text instructionText;

    [Tooltip("Optional anchor for canvas placement (for example, Main Camera). If empty, this GameObject transform is used.")]
    public Transform uiAnchor;

    [Min(0.1f)]
    public float canvasDistance = 1.75f;

    public float canvasVerticalOffset = 0f;

    [SerializeField]
    private bool autoStartOnEnable = false;

    private Coroutine activeSequence;
    private bool isRunning;
    private Vector3 initialForwardFlat;

    public bool IsRunning => isRunning;

    private void OnEnable()
    {
        if (autoStartOnEnable)
        {
            StartSequence();
        }
    }

    private void Update()
    {
        UpdateInstructionCanvasPlacement();
    }

    [ContextMenu("Start Sequence")]
    public void StartSequence()
    {
        if (activeSequence != null)
        {
            StopCoroutine(activeSequence);
            activeSequence = null;
        }

        activeSequence = StartCoroutine(RunSequence());
    }

    public void StopSequence()
    {
        if (activeSequence != null)
        {
            StopCoroutine(activeSequence);
            activeSequence = null;
        }

        isRunning = false;
        SetInstructionText(string.Empty);
    }

    private IEnumerator RunSequence()
    {
        if (steps == null || steps.Count == 0)
        {
            SetInstructionText("Done — thank you!");
            yield break;
        }

        isRunning = true;
        initialForwardFlat = transform.forward;
        initialForwardFlat.y = 0f;
        if (initialForwardFlat.sqrMagnitude < 0.0001f)
        {
            initialForwardFlat = Vector3.forward;
        }
        initialForwardFlat.Normalize();

        for (int i = 0; i < steps.Count; i++)
        {
            MoveStep step = steps[i];
            yield return MoveOneStep(step);

            if (pauseBetweenSteps > 0f && i < steps.Count - 1)
            {
                float remainingPause = pauseBetweenSteps;
                MoveStep nextStep = steps[i + 1];
                while (remainingPause > 0f)
                {
                    SetInstructionText($"Next: MOVE {nextStep.direction.ToString().ToUpperInvariant()} ({remainingPause:0.0}s)");
                    remainingPause -= Time.deltaTime;
                    yield return null;
                }
            }
        }

        SetInstructionText("Done — thank you!");
        isRunning = false;
        activeSequence = null;
    }

    private IEnumerator MoveOneStep(MoveStep step)
    {
        Vector3 moveDir = GetDirectionVector(step.direction);
        Vector3 displacement = moveDir * Mathf.Max(0f, step.distanceMeters);

        Vector3 startPos = transform.position;
        Vector3 endPos = startPos + displacement;
        float duration = Mathf.Max(0.01f, step.durationSeconds);

        float elapsed = 0f;
        while (elapsed < duration)
        {
            elapsed += Time.deltaTime;
            float t = Mathf.Clamp01(elapsed / duration);
            Vector3 targetPos = Vector3.Lerp(startPos, endPos, t);
            MoveTo(targetPos);

            float remaining = Mathf.Max(0f, duration - elapsed);
            SetInstructionText($"MOVE {step.direction.ToString().ToUpperInvariant()} ({remaining:0.0}s)");
            yield return null;
        }

        MoveTo(endPos);
        SetInstructionText($"MOVE {step.direction.ToString().ToUpperInvariant()} (0.0s)");
    }

    private Vector3 GetDirectionVector(MoveDirection direction)
    {
        Vector3 flatRight = Vector3.Cross(Vector3.up, initialForwardFlat).normalized;

        switch (direction)
        {
            case MoveDirection.Forward:
                return initialForwardFlat;
            case MoveDirection.Back:
                return -initialForwardFlat;
            case MoveDirection.Left:
                return -flatRight;
            case MoveDirection.Right:
                return flatRight;
            default:
                return initialForwardFlat;
        }
    }

    private void MoveTo(Vector3 worldPos)
    {
        if (characterController != null)
        {
            Vector3 delta = worldPos - transform.position;
            characterController.Move(delta);
            return;
        }

        transform.position = worldPos;
    }

    private void UpdateInstructionCanvasPlacement()
    {
        if (instructionCanvas == null)
        {
            return;
        }

        Transform anchor = uiAnchor != null ? uiAnchor : transform;
        Vector3 anchorForward = anchor.forward;
        anchorForward.y = 0f;
        if (anchorForward.sqrMagnitude < 0.0001f)
        {
            anchorForward = transform.forward;
            anchorForward.y = 0f;
        }
        anchorForward.Normalize();

        Vector3 anchorPos = anchor.position + Vector3.up * canvasVerticalOffset;
        instructionCanvas.position = anchorPos + anchorForward * canvasDistance;

        Vector3 toPlayer = anchor.position - instructionCanvas.position;
        if (toPlayer.sqrMagnitude > 0.0001f)
        {
            instructionCanvas.rotation = Quaternion.LookRotation(toPlayer.normalized, Vector3.up);
        }
    }

    private void SetInstructionText(string value)
    {
        if (instructionText != null)
        {
            instructionText.text = value;
        }
    }
}
