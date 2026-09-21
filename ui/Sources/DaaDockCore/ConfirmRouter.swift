import Foundation

/// What to do with an incoming `confirm.request`.
public enum ConfirmAdmission: Sendable, Equatable {
    /// Put this card on screen.
    case present(ApprovalCard)
    /// Answer immediately with a refusal. The card never appears.
    case refuse(id: String, ApprovalOutcome, why: String)
    /// Send nothing at all. Used only for a replayed token, whose original
    /// has already been answered — replying again would produce a second
    /// `confirm.result` for one `confirm.request`.
    case drop(id: String, why: String)
}

/// The boundary between the wire and the screen.
///
/// Lives in the pure core rather than in the AppKit layer because this is
/// where the safety-critical decisions are: whether a frame is renderable at
/// all, whether a token has been seen before, and what happens when a second
/// card arrives while one is up. None of them is a pixel decision and none of
/// them should need a running app to test.
public enum ConfirmRouter {

    public static func admit(
        id: String,
        params: JSONValue,
        pending: PendingApprovals,
        cardAlreadyOpen: Bool
    ) -> ConfirmAdmission {

        // 1. Renderable, or refused. A card assembled from a frame we cannot
        //    read in full would still look like a card, and a card someone
        //    approves is a consent record. There is no such thing as consent
        //    to something that was not shown.
        guard let card = ApprovalCard(id: id, params: params) else {
            return .refuse(id: id, .unreadable, why: "the request could not be rendered in full")
        }

        // 2. Single-use tokens. An unknown, consumed or stale id is dropped.
        switch pending.admit(card) {
        case .replayed:
            return .drop(id: id, why: "this confirmation token was already used")
        case .duplicate:
            return .drop(id: id, why: "this confirmation is already on screen")
        case .admitted:
            break
        }

        // 3. One card at a time. A second card appearing over the first would
        //    let a click aimed at one land on the other — the exact
        //    click-through the 400 ms inert window exists to prevent, arriving
        //    by a different route. Queueing is not safe either: the queued
        //    card's countdown would run while it was invisible. So the second
        //    one is refused, and Python is free to ask again.
        if cardAlreadyOpen {
            _ = pending.consume(id)
            return .refuse(id: id, .cancelled,
                           why: "another approval is already on screen")
        }

        return .present(card)
    }
}
