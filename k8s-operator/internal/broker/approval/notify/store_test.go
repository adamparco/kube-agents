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

package notify_test

import (
	"context"
	"errors"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/approval/notify"
)

func storeScheme(t *testing.T) *runtime.Scheme {
	t.Helper()
	s := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(s); err != nil {
		t.Fatalf("add clientgo scheme: %v", err)
	}
	return s
}

func TestConfigMapStoreGetMissingIsNotAnError(t *testing.T) {
	c := fake.NewClientBuilder().WithScheme(storeScheme(t)).Build()
	store := &notify.ConfigMapStore{Client: c, Name: "delivery-state", Namespace: "kubeagents-system"}

	_, found, err := store.Get(context.Background(), "ar-1")
	if err != nil {
		t.Fatalf("Get on a missing configmap should not error: %v", err)
	}
	if found {
		t.Error("expected found=false")
	}
}

func TestConfigMapStoreGetSurfacesANonNotFoundError(t *testing.T) {
	c := fake.NewClientBuilder().
		WithScheme(storeScheme(t)).
		WithInterceptorFuncs(interceptor.Funcs{
			Get: func(context.Context, client.WithWatch, client.ObjectKey, client.Object, ...client.GetOption) error {
				return apierrors.NewInternalError(errors.New("etcd is unavailable"))
			},
		}).
		Build()
	store := &notify.ConfigMapStore{Client: c, Name: "delivery-state", Namespace: "kubeagents-system"}

	_, _, err := store.Get(context.Background(), "ar-1")
	if err == nil {
		t.Fatal("expected a transport error to surface rather than be treated as \"not found\"")
	}
	if !strings.Contains(err.Error(), "reading delivery state configmap") {
		t.Errorf("error = %q, want it to name what failed", err.Error())
	}
}

func TestConfigMapStoreGetSurfacesAnUndecodableEntry(t *testing.T) {
	c := fake.NewClientBuilder().WithScheme(storeScheme(t)).WithObjects(&corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{Name: "delivery-state", Namespace: "kubeagents-system"},
		Data:       map[string]string{"ar-1": "not json"},
	}).Build()
	store := &notify.ConfigMapStore{Client: c, Name: "delivery-state", Namespace: "kubeagents-system"}

	_, _, err := store.Get(context.Background(), "ar-1")
	if err == nil {
		t.Fatal("expected a decode error for a corrupt entry")
	}
	if !strings.Contains(err.Error(), "decoding delivery state") {
		t.Errorf("error = %q, want it to name what failed", err.Error())
	}
}

func TestConfigMapStoreSaveThenGetRoundTrips(t *testing.T) {
	c := fake.NewClientBuilder().WithScheme(storeScheme(t)).Build()
	store := &notify.ConfigMapStore{Client: c, Name: "delivery-state", Namespace: "kubeagents-system"}

	want := notify.DeliveryState{Platform: notify.PlatformSlack, Channel: "C01", Ref: "123.456", Key: "gen-1"}
	if err := store.Save(context.Background(), "ar-1", want); err != nil {
		t.Fatalf("Save: %v", err)
	}

	got, found, err := store.Get(context.Background(), "ar-1")
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	if !found || got != want {
		t.Errorf("got %+v found=%v, want %+v found=true", got, found, want)
	}
}

func TestConfigMapStoreSaveIsKeyedPerRecord(t *testing.T) {
	c := fake.NewClientBuilder().WithScheme(storeScheme(t)).Build()
	store := &notify.ConfigMapStore{Client: c, Name: "delivery-state", Namespace: "kubeagents-system"}

	if err := store.Save(context.Background(), "ar-1", notify.DeliveryState{Ref: "one"}); err != nil {
		t.Fatalf("Save ar-1: %v", err)
	}
	if err := store.Save(context.Background(), "ar-2", notify.DeliveryState{Ref: "two"}); err != nil {
		t.Fatalf("Save ar-2: %v", err)
	}

	got1, _, _ := store.Get(context.Background(), "ar-1")
	got2, _, _ := store.Get(context.Background(), "ar-2")
	if got1.Ref != "one" || got2.Ref != "two" {
		t.Errorf("records overwrote each other: ar-1=%q ar-2=%q", got1.Ref, got2.Ref)
	}
}
