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

package main

import (
	"context"
	"time"

	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	ctrllog "sigs.k8s.io/controller-runtime/pkg/log"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/pipeline"
)

// resumePollInterval bounds how long a just-approved action can sit before this broker notices.
// Short enough that "approve" in chat feels like it did something; long enough that a fleet of
// agents polling their own namespace is not itself a load concern -- ActionRecords needing this
// loop's attention (PendingApproval or freshly-Pending) are, by construction, rare and short-lived.
const resumePollInterval = 5 * time.Second

// runResumeLoop drives pipeline.ResumeController without a controller-runtime manager or watch —
// see the call site in run() for why. It exits when ctx is done; the caller does not wait on it,
// because the resumption loop racing an hours-long TTL has nothing useful to say about broker
// shutdown that the HTTP server's own graceful-shutdown handling does not already say better.
func runResumeLoop(ctx context.Context, c client.Client, pipe *pipeline.Pipeline, records pipeline.RecordStore, namespace string) {
	log := ctrllog.Log.WithName("resume")
	rc := &pipeline.ResumeController{Client: c, Pipeline: pipe, Records: records}

	ticker := time.NewTicker(resumePollInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			resumeSweep(ctx, c, rc, namespace, log)
		}
	}
}

// reconciler is the one method resumeSweep needs from pipeline.ResumeController, named locally so
// a test can pass a recording fake instead of standing up a full Pipeline.
type reconciler interface {
	Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error)
}

func resumeSweep(ctx context.Context, c client.Client, rc reconciler, namespace string, log logSink) {
	var list agentv1alpha1.ActionRecordList
	if err := c.List(ctx, &list, client.InNamespace(namespace)); err != nil {
		log.Error(err, "resume: listing action records")
		return
	}
	for i := range list.Items {
		ar := &list.Items[i]
		switch ar.Status.Phase {
		case agentv1alpha1.PhasePending, agentv1alpha1.PhasePendingApproval:
			// fall through to reconcile
		default:
			continue
		}
		if _, err := rc.Reconcile(ctx, ctrl.Request{NamespacedName: client.ObjectKeyFromObject(ar)}); err != nil {
			log.Error(err, "resume: reconciling", "record", ar.Name)
		}
	}
}

// logSink is the one method this file needs from logr.Logger, named locally so resumeSweep's
// signature does not commit callers (including tests) to importing logr just to pass one in.
type logSink interface {
	Error(err error, msg string, keysAndValues ...any)
}
