using NUnit.Framework;
using BCI;

namespace BCI.Tests
{
    public class BCICommandLogicTests
    {
        [TestCase("LEFT", true)]
        [TestCase("RIGHT", true)]
        [TestCase("FORWARD", true)]
        [TestCase("BACKWARD", true)]
        [TestCase("left", false)]
        [TestCase("UP", false)]
        [TestCase("", false)]
        [TestCase(null, false)]
        public void IsValidLabel_returns_expected(string label, bool expected)
        {
            Assert.AreEqual(expected, BCICommandLogic.IsValidLabel(label));
        }

        [Test]
        public void TryParse_valid_message_returns_true_and_populates_fields()
        {
            string json = "{\"label\":\"LEFT\",\"confidence\":0.87,\"ts\":1714000000.123}";
            bool ok = BCICommandLogic.TryParse(json, out BCIMessage msg);

            Assert.IsTrue(ok);
            Assert.AreEqual("LEFT", msg.label);
            Assert.AreEqual(0.87f, msg.confidence, 1e-4);
            Assert.AreEqual(1714000000.123, msg.ts, 1e-3);
        }

        [Test]
        public void TryParse_malformed_json_returns_false()
        {
            bool ok = BCICommandLogic.TryParse("{not json", out BCIMessage _);
            Assert.IsFalse(ok);
        }

        [Test]
        public void TryParse_empty_string_returns_false()
        {
            bool ok = BCICommandLogic.TryParse("", out BCIMessage _);
            Assert.IsFalse(ok);
        }

        [Test]
        public void TryParse_null_returns_false()
        {
            bool ok = BCICommandLogic.TryParse(null, out BCIMessage _);
            Assert.IsFalse(ok);
        }

        [Test]
        public void IsAccepted_above_threshold_with_valid_label_is_true()
        {
            var msg = new BCIMessage { label = "LEFT", confidence = 0.7f };
            Assert.IsTrue(BCICommandLogic.IsAccepted(msg, 0.6f, true));
        }

        [Test]
        public void IsAccepted_below_threshold_is_false()
        {
            var msg = new BCIMessage { label = "LEFT", confidence = 0.5f };
            Assert.IsFalse(BCICommandLogic.IsAccepted(msg, 0.6f, true));
        }

        [Test]
        public void IsAccepted_exactly_at_threshold_is_true()
        {
            var msg = new BCIMessage { label = "LEFT", confidence = 0.6f };
            Assert.IsTrue(BCICommandLogic.IsAccepted(msg, 0.6f, true));
        }

        [Test]
        public void IsAccepted_invalid_label_is_false_even_if_high_confidence()
        {
            var msg = new BCIMessage { label = "UP", confidence = 0.99f };
            Assert.IsFalse(BCICommandLogic.IsAccepted(msg, 0.6f, true));
        }

        [Test]
        public void IsAccepted_threshold_zero_accepts_any_valid_label()
        {
            var msg = new BCIMessage { label = "RIGHT", confidence = 0.0f };
            Assert.IsTrue(BCICommandLogic.IsAccepted(msg, 0.0f, true));
        }

        [Test]
        public void ProjectHorizontal_zeros_y_and_normalizes()
        {
            var v = new UnityEngine.Vector3(3f, 5f, 4f);
            var p = BCICommandLogic.ProjectHorizontal(v);
            Assert.AreEqual(0f, p.y, 1e-5);
            Assert.AreEqual(1f, p.magnitude, 1e-5);
            Assert.AreEqual(0.6f, p.x, 1e-4);
            Assert.AreEqual(0.8f, p.z, 1e-4);
        }

        [Test]
        public void ProjectHorizontal_pure_vertical_returns_world_forward_fallback()
        {
            var v = new UnityEngine.Vector3(0f, 1f, 0f);
            var p = BCICommandLogic.ProjectHorizontal(v);
            Assert.AreEqual(0f, p.x, 1e-5);
            Assert.AreEqual(0f, p.y, 1e-5);
            Assert.AreEqual(1f, p.z, 1e-5);
        }

        [Test]
        public void ComputeMovement_FORWARD_steps_along_head_forward_no_yaw()
        {
            var fwd = new UnityEngine.Vector3(0f, 0f, 1f);
            BCICommandLogic.ComputeMovement("FORWARD", fwd, 0.5f,
                out UnityEngine.Vector3 dPos, out float dYaw);
            Assert.AreEqual(0f, dYaw, 1e-4);
            Assert.AreEqual(0f, dPos.x, 1e-4);
            Assert.AreEqual(0f, dPos.y, 1e-4);
            Assert.AreEqual(0.5f, dPos.z, 1e-4);
        }

        [Test]
        public void ComputeMovement_BACKWARD_steps_opposite_no_yaw()
        {
            var fwd = new UnityEngine.Vector3(0f, 0f, 1f);
            BCICommandLogic.ComputeMovement("BACKWARD", fwd, 0.5f,
                out UnityEngine.Vector3 dPos, out float dYaw);
            Assert.AreEqual(0f, dYaw, 1e-4);
            Assert.AreEqual(-0.5f, dPos.z, 1e-4);
            Assert.AreEqual(0f, dPos.x, 1e-4);
        }

        [Test]
        public void ComputeMovement_LEFT_yaws_minus90_then_steps()
        {
            var fwd = new UnityEngine.Vector3(0f, 0f, 1f);
            BCICommandLogic.ComputeMovement("LEFT", fwd, 0.5f,
                out UnityEngine.Vector3 dPos, out float dYaw);
            Assert.AreEqual(-90f, dYaw, 1e-4);
            Assert.AreEqual(-0.5f, dPos.x, 1e-4);
            Assert.AreEqual(0f, dPos.y, 1e-4);
            Assert.AreEqual(0f, dPos.z, 1e-4);
        }

        [Test]
        public void ComputeMovement_RIGHT_yaws_plus90_then_steps()
        {
            var fwd = new UnityEngine.Vector3(0f, 0f, 1f);
            BCICommandLogic.ComputeMovement("RIGHT", fwd, 0.5f,
                out UnityEngine.Vector3 dPos, out float dYaw);
            Assert.AreEqual(90f, dYaw, 1e-4);
            Assert.AreEqual(0.5f, dPos.x, 1e-4);
            Assert.AreEqual(0f, dPos.z, 1e-4);
        }

        [Test]
        public void ComputeMovement_step_size_scales_position()
        {
            var fwd = new UnityEngine.Vector3(0f, 0f, 1f);
            BCICommandLogic.ComputeMovement("FORWARD", fwd, 1.5f,
                out UnityEngine.Vector3 dPos, out float _);
            Assert.AreEqual(1.5f, dPos.z, 1e-4);
        }
    }
}
