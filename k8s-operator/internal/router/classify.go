/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package router

import "errors"

// IsDeterministicRefusal reports whether err is one of the router's terminal, deterministic refusals
// (unaddressed/malformed/unknown-tier/inference-disabled/missing-project/clarify/no-such-target/
// unauthorized). These outcomes are a pure function of the message and the current index, so
// redelivering the same event cannot change them — the inbound receiver Acks them (turn handled).
//
// Anything else (notably a dispatch/publish failure) is transient: the receiver Nacks so Pub/Sub
// redelivers. Keeping the classification here, next to the sentinels, means the delivery layer can't
// drift from the set of refusals the gateway can actually return.
func IsDeterministicRefusal(err error) bool {
	if err == nil {
		return false
	}
	for _, sentinel := range []error{
		ErrUnaddressed,
		ErrMalformedHandle,
		ErrUnknownTier,
		ErrInferenceUnavailable,
		ErrMissingProjectContext,
		ErrClarify,
		ErrNoSuchTarget,
		ErrUnauthorized,
	} {
		if errors.Is(err, sentinel) {
			return true
		}
	}
	return false
}
