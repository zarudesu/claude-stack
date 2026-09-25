// Package svc implements a small majority-vote quorum check.
package svc

// Verdict returns "fit" when fit votes outnumber unfit votes, "unfit" when
// the reverse holds, and "unknown" on a tie.
func Verdict(fit, unfit int) string {
	if fit > unfit {
		return "fit"
	}
	if unfit > fit {
		return "unfit"
	}
	return "unknown"
}
