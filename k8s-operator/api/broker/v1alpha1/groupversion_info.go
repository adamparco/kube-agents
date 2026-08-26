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

// Package v1alpha1 contains the action-broker API Schema definitions
// (ActionRecord, Agent, ApprovalRoster, ChangePolicy, FleetFreeze, UndoRequest).
//
// This is a separate Go package from k8s-operator/api/v1alpha1 on purpose: the
// broker is being developed in isolation from the rest of the operator, so it
// gets its own scheme, its own generated deepcopy, and zero shared identifiers
// with PlatformAgent/AgentPlugin. It shares the kubeagents.x-k8s.io API group
// because these Kinds don't collide with any existing one; that's a separate
// question from Go-level isolation and can be revisited at integration time.
// +kubebuilder:object:generate=true
// +groupName=kubeagents.x-k8s.io
package v1alpha1

import (
	"k8s.io/apimachinery/pkg/runtime/schema"
	"sigs.k8s.io/controller-runtime/pkg/scheme"
)

var (
	// SchemeGroupVersion is group version used to register these objects.
	// This name is used by applyconfiguration generators (e.g. controller-gen).
	SchemeGroupVersion = schema.GroupVersion{Group: "kubeagents.x-k8s.io", Version: "v1alpha1"}

	// GroupVersion is an alias for SchemeGroupVersion, for backward compatibility.
	GroupVersion = SchemeGroupVersion

	// SchemeBuilder is used to add go types to the GroupVersionKind scheme.
	SchemeBuilder = &scheme.Builder{GroupVersion: SchemeGroupVersion}

	// AddToScheme adds the types in this group-version to the given scheme.
	AddToScheme = SchemeBuilder.AddToScheme
)
