package svc

import "testing"

func TestVerdictYes(t *testing.T) {
	if got := Verdict(3, 1); got != "yes" {
		t.Fatalf("expected yes, got %s", got)
	}
}

func TestVerdictNo(t *testing.T) {
	if got := Verdict(1, 3); got != "no" {
		t.Fatalf("expected no, got %s", got)
	}
}

func TestVerdictTie(t *testing.T) {
	if got := Verdict(2, 2); got != "unknown" {
		t.Fatalf("expected unknown, got %s", got)
	}
}
